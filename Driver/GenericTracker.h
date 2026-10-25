#pragma once

#include <openvr_driver.h>
#include <string>
#include <memory>
#include <thread>
#include <atomic>
#include <mutex>
#include <condition_variable>
#include <cstdint>
#include <vector>
#include "AngularVelocity.h"
#include "CameraStream.h"
#include "CalibrationTracker.h"
#include "FloorLevel.h"

// Main Xvisio SDK header, found through the project's $(XvsdkDir)\include directory.
#include <xvsdk/xv-sdk.h>

struct HmdSlamData
{
    // Position
    double x, y, z;
    // Orientation (Quaternion: w, x, y, z)
    double qw, qx, qy, qz;

    // Linear Velocity (m/s)
    double vx, vy, vz;
    // Angular Velocity (rad/s)
    double wx, wy, wz;

    // Pose confidence [0,1], 0 = tracking lost
    double confidence;
    // Host timestamp of the physical sensor measurement (seconds, steady_clock
    // domain). This is diagnostic metadata; getPose() itself returns a pose for
    // "now + prediction", so this timestamp is not the poseTimeOffset.
    double hostTimestamp;
};

// All tunables/calibration read from steamvr.vrsettings [BSB-SLAM].
// Guarded by m_configMutex: written by the UDP thread (LoadConfig), read by the pose thread.
struct TrackerConfig
{
    // Increment whenever the raw Xvisio-to-OpenVR basis mapping changes. Old
    // mount/lever/world calibration values are not valid under a new basis.
    int calibration_version{0};

    // Lever arm L: vector from the XR50 device origin to the HMD head origin,
    // expressed in the (coordinate-mapped) device body frame.
    //   p_head = p_device + R(q_device) * L
    // NOTE: sign convention changed vs. old builds (old code did p - R*offset).
    // Re-run calibrate.py after updating.
    // Cover CAD profile; includes the tracking point 2 mm beyond the datum hole.
    double lever_x{0.0000795101}, lever_y{0.0056645177}, lever_z{0.0516054844};

    // Mount rotation X: constant BODY-frame rotation from HMD frame to device frame,
    // applied as a right-multiply:  q_head = q_device * X.
    // The XR50 is installed upside-down around its optical/forward axis on the Beyond,
    // with a 10 degree downward optical pitch in the Cover CAD profile.
    // See profiles/beyond-cover-xr50.json for measurement provenance.
    double mount_qw{0.0}, mount_qx{0.0}, mount_qy{0.0871557427}, mount_qz{0.9961946981};

    // World transform (C, t): constant WORLD-frame yaw + translation mapping the SLAM
    // map frame into the calibrated OpenVR universe. Only meaningful when the SLAM map
    // itself persists across sessions (CSLAM reference map) -- otherwise the map origin
    // is re-rolled every boot and this would be stale. Gated on m_bWorldTransformActive.
    double world_qw{1.0}, world_qx{0.0}, world_qy{0.0}, world_qz{0.0};
    double world_x{0.0}, world_y{0.0}, world_z{0.0};
    bool   world_valid{false};

    // Manual extra SteamVR prediction in seconds. Positive makes the runtime
    // extrapolate farther; negative reduces runtime extrapolation.
    double time_offset{0.0};

    // Optional Xvisio SDK forward prediction (getPose(pose, prediction_s)).
    // 0 = disabled. Only engaged after a warmup period because the SDK predictor
    // crashes if invoked before the IMU state is initialized.
    // Default to an 8 ms SDK horizon after warmup: the SDK's IMU-driven predictor
    // covers the first part, SteamVR's constant-velocity extrapolation the rest. SteamVR
    // receives the corresponding future pose timestamp, so it does not predict twice.
    double prediction_s{0.008};

    // Feed angular velocity to SteamVR only after its frame has been validated
    // against pose changes. Toggle live with SET_ANGULAR_VELOCITY 0/1.
    bool use_angular_velocity{true};

    // Valve documents vecAngularVelocity in driver world space, but SteamVR may apply
    // it in the device's local frame. In the wrong frame, yaw (shared by both) still
    // predicts correctly while pitch and roll do not. Toggle live with SET_ANGULAR_LOCAL 0/1.
    bool angular_velocity_local{false};

    // Fallback startup height (m): when no world calibration is active, the pose height
    // at settle time is assumed to be this far above the floor. 0.0 reproduces the old
    // "headset starts on the floor" behavior.
    double head_height{0.0};

    // Load a persisted CSLAM reference map at startup if one exists.
    bool use_cslam{true};
};

class CGenericTracker : public vr::ITrackedDeviceServerDriver
{
public:
    static constexpr int kCalibrationVersion = 2;

    explicit CGenericTracker(CCalibrationTracker* calibrationTracker = nullptr);
    virtual ~CGenericTracker();

    virtual vr::EVRInitError Activate(uint32_t unObjectId) override;
    virtual void Deactivate() override;
    virtual void EnterStandby() override;
    virtual void* GetComponent(const char* pchComponentNameAndVersion) override;
    virtual void DebugRequest(const char* pchRequest, char* pchResponseBuffer, uint32_t unResponseBufferSize) override;
    virtual vr::DriverPose_t GetPose() override;

    void RunFrame();
    const std::string& GetSerialNumber() const;
    static vr::HmdQuaternion_t NormalizeQuaternion(vr::HmdQuaternion_t q);
    static vr::HmdQuaternion_t MultiplyQuaternions(vr::HmdQuaternion_t a, vr::HmdQuaternion_t b);
    static vr::HmdVector3d_t RotateVectorByQuaternion(const vr::HmdVector3d_t& vec, const vr::HmdQuaternion_t& quat);

    void LoadConfig();

private:
    CCalibrationTracker* m_calibrationTracker; // provider-owned; outlives pose thread
    vr::TrackedDeviceIndex_t m_unObjectId;
    vr::PropertyContainerHandle_t m_ulPropertyContainer;

    vr::DriverPose_t m_pose;      // last submitted pose; guarded by m_poseMutex
    std::mutex m_poseMutex;

    TrackerConfig m_config;
    std::mutex m_configMutex;

    // ---- Startup / floor state (owned by the pose thread; atomics for UDP access) ----
    std::atomic<int>    m_settleCounter{0};       // consecutive confident frames since SLAM start
    std::atomic<bool>   m_bSettled{false};
    std::atomic<bool>   m_bFloorCaptured{false};
    std::atomic<double> m_floorY0{0.0};           // raw head Y captured at settle (or via SET_FLOOR)
    std::atomic<double> m_heightAdd{0.0};         // height above floor assumed at capture time
    // Floor calibration is a session-only correction from two seconds of the headset
    // resting still on the floor: gravity levels the frame (m_level), then height.
    std::atomic<double> m_floorAdjustY{0.0};
    // 0 ready, 1 sampling, 2 calibrated, 3 timed out, 4 unavailable, 5 stopped, 6 unstable.
    std::atomic<int> m_floorScanStatus{0};
    std::atomic<double> m_floorScanHeight{0.0};
    std::atomic<double> m_floorScanStarted{0.0};
    bool m_groundCaptureActive{false}; // pose thread only
    double m_groundCaptureStarted{0.0};
    double m_groundLastSensorTimestamp{0.0};
    int m_groundSamples{0};
    floor_level::Vec3 m_groundSum{0, 0, 0};   // unleveled head positions
    floor_level::Vec3 m_groundUpSum{0, 0, 0}; // accelerometer "up" in the unleveled world
    int m_groundImuSamples{0};
    vr::HmdVector3d_t m_groundMinPosition{};
    vr::HmdVector3d_t m_groundMaxPosition{};
    vr::HmdQuaternion_t m_groundStartRotation{1.0, 0.0, 0.0, 0.0};
    double m_groundMinRotationDot{1.0};
    // Gravity leveling from the last floor calibration, applied before m_floorAdjustY.
    floor_level::Level m_level;               // guarded by m_levelMutex
    std::mutex m_levelMutex;
    std::atomic<double> m_accelNormReport{0.0}; // |accel| measured at that calibration
    std::atomic<bool> m_levelReady{false};
    std::atomic<double> m_levelDegrees{0.0};  // correction currently applied
    // Accelerometer sums since the last pose tick. The SDK IMU thread adds; the pose thread drains.
    std::mutex m_imuMutex;
    int m_imuSamples{0};
    double m_imuSum[3]{0.0, 0.0, 0.0};
    int m_imuCallbackId{-1}; // init thread registers it once SLAM has settled
    void RegisterImuCallback();
    std::atomic<double> m_lastConfidence{0.0};
    std::atomic<double> m_lastSampleAgeMs{0.0};   // physical sensor age; diagnostic only
    std::atomic<double> m_lastPoseOffsetMs{0.0};  // value most recently sent to OpenVR
    std::atomic<double> m_lastGetPoseMs{0.0};
    std::atomic<double> m_lastPoseTickMs{0.0};
    std::atomic<double> m_lastAppliedPredictionMs{0.0};
    std::atomic<double> m_lastCallbackTime{0.0};
    std::atomic<double> m_lastCallbackToSubmitMs{0.0};
    // Age of the SDK's own filtered pose when its callback arrives. getPose stamps its
    // result "call time + prediction", which hides how old the sensor data really is.
    std::atomic<double> m_lastRawPoseAgeMs{-1.0};
    std::atomic<int> m_angularFrame{0}; // 0 unknown, 1 world, 2 body
    angular_velocity::Selector m_angularSelector; // pose thread only
    std::atomic<int> m_linearFrame{0};  // 0 unknown, 1 world, 2 body
    angular_velocity::LinearSelector m_linearSelector; // pose thread only
    double m_firstValidTime{0.0};                 // pose-thread-local; steady seconds of first valid pose

    // CSLAM / world transform
    std::atomic<bool> m_bCslamLoadAttempted{false};
    std::atomic<bool> m_bWorldTransformActive{false};

    std::string m_sSerialNumber;
    std::string m_sModelNumber;

    std::shared_ptr<xv::Device> m_pDevice;
    CameraStream m_cameraStream;
    int m_cameraCallbackId{-1}; // UDP thread owns registration
    std::shared_ptr<xv::Slam> m_pSlam;
    int m_slamPoseCallbackId{-1};
    // Published true (release) by InitThreadFunction only after m_pSlam is fully assigned
    // and started; read (acquire) by the pose thread before touching m_pSlam. Prevents a
    // torn read of the shared_ptr during the ~12s background init.
    std::atomic<bool> m_bSlamReady{false};

    std::thread m_initThread;
    bool m_bStopInitThread;   // plain bool: xv::getDevices takes a bool*
    void InitThreadFunction();
    void TryLoadCslamMap();

    // Dedicated high-rate pose thread. Woken by every SDK pose/IMU callback, then
    // fetches, transforms, and submits directly -- decoupled from per-vsync RunFrame.
    std::thread m_poseThread;
    std::atomic<bool> m_bStopPoseThread{false};
    std::mutex m_poseWakeMutex;
    std::condition_variable m_poseWakeCv;
    std::atomic<uint64_t> m_poseWakeSequence{0};
    void PoseThreadFunction();
    void PoseTick();
    void BuildAndSubmitPose(const HmdSlamData& slamData, double sdkPoseTimeSeconds);

    // UDP IPC for calibration/config (port 8421)
    std::thread m_udpThread;
    std::atomic<bool> m_bStopUdpThread{false};
    std::atomic<bool> m_bStreamCalibrationData{false};
    std::atomic<uintptr_t> m_udpSocket{0}; // Cast to SOCKET in cpp
    // We store the client address when we receive START_STREAMING so we know where to send data back
    char m_clientIp[64];
    uint16_t m_clientPort{0};
    void UdpThreadFunction();
    void SaveCslamMap();

    void StreamCalibrationPacket(const xv::Pose& slam_pose);
    static std::string GetMapPath();
    static double SteadyNowSeconds();
};

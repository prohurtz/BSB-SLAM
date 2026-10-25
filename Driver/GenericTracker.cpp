#define NOMINMAX
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#pragma comment(lib, "Ws2_32.lib")

#include "GenericTracker.h"
#include <cstdarg>   // For va_list, va_start, va_end
#include <objbase.h> // For CoInitializeEx
#include <chrono>
#include <cmath>
#include <fstream>
#include <algorithm>
#include <iomanip>
#include <sstream>

// Helper to log messages to the SteamVR driver log
// You can view these logs in SteamVR's web console under `vrserver.txt`
void Log(const char* pchFormat, ...)
{
    if (!pchFormat) return;
    char buf[1024];
    va_list args;
    va_start(args, pchFormat);
    int len = vsnprintf(buf, sizeof(buf) - 2, pchFormat, args);
    va_end(args);

    // vsnprintf returns the length it *would* have written; clamp to what actually fits
    // so we never index past the buffer when appending the trailing newline.
    if (len > 0 && len < (int)sizeof(buf) - 1) {
        if (buf[len - 1] != '\n') {
            buf[len] = '\n';
            buf[len + 1] = '\0';
        }
    }

    if (vr::VRDriverLog())
    {
        vr::VRDriverLog()->Log(buf);
    }
}

static vr::HmdVector3d_t CrossProduct(const vr::HmdVector3d_t& a, const vr::HmdVector3d_t& b)
{
    vr::HmdVector3d_t r;
    r.v[0] = a.v[1] * b.v[2] - a.v[2] * b.v[1];
    r.v[1] = a.v[2] * b.v[0] - a.v[0] * b.v[2];
    r.v[2] = a.v[0] * b.v[1] - a.v[1] * b.v[0];
    return r;
}

double CGenericTracker::SteadyNowSeconds()
{
    // Same clock domain as xv::Pose::hostTimestamp() (std::chrono::steady_clock).
    return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
}

std::string CGenericTracker::GetMapPath()
{
    char* localAppData = nullptr;
    size_t len = 0;
    std::string path;
    if (_dupenv_s(&localAppData, &len, "LOCALAPPDATA") == 0 && localAppData)
    {
        path = std::string(localAppData) + "\\BSB-SLAM";
        free(localAppData);
        CreateDirectoryA(path.c_str(), nullptr);
        path += "\\slam_map.bin";
    }
    return path;
}

CGenericTracker::CGenericTracker(CCalibrationTracker* calibrationTracker)
    : m_calibrationTracker(calibrationTracker)
{
    m_bStopInitThread = false;
    m_unObjectId = vr::k_unTrackedDeviceIndexInvalid;
    m_ulPropertyContainer = 0;

    m_sSerialNumber = "SLAM_TRACKER_001";
    m_sModelNumber = "Xvisio XR50 Tracker";

    m_pDevice = nullptr;
    m_pSlam = nullptr;

    memset(m_clientIp, 0, sizeof(m_clientIp));

    // Initialize the pose to a valid, default state.
    m_pose = { 0 };
    m_pose.qRotation = { 1.0, 0.0, 0.0, 0.0 };
    m_pose.qWorldFromDriverRotation = { 1.0, 0.0, 0.0, 0.0 };
    m_pose.qDriverFromHeadRotation = { 1.0, 0.0, 0.0, 0.0 };
    m_pose.vecPosition[0] = 0.0;
    m_pose.vecPosition[1] = 0.1;
    m_pose.vecPosition[2] = 0.0;
    m_pose.poseIsValid = false;
    m_pose.willDriftInYaw = false;
    m_pose.shouldApplyHeadModel = false;
    m_pose.result = vr::TrackingResult_Uninitialized;
    m_pose.deviceIsConnected = false;
}

CGenericTracker::~CGenericTracker()
{
    // Cleanup can also arrive without a preceding device Deactivate callback.
    if (m_initThread.joinable() || m_poseThread.joinable() || m_udpThread.joinable())
        Deactivate();
}

const std::string& CGenericTracker::GetSerialNumber() const {
    return m_sSerialNumber;
}

vr::EVRInitError CGenericTracker::Activate(uint32_t unObjectId)
{
    Log("CGenericTracker::Activate called");
    m_unObjectId = unObjectId;
    m_ulPropertyContainer = vr::VRProperties()->TrackedDeviceToPropertyContainer(m_unObjectId);

    vr::VRProperties()->SetStringProperty(m_ulPropertyContainer, vr::Prop_TrackingSystemName_String, "xvisio_slam");
    vr::VRProperties()->SetStringProperty(m_ulPropertyContainer, vr::Prop_ModelNumber_String, m_sModelNumber.c_str());
    vr::VRProperties()->SetStringProperty(m_ulPropertyContainer, vr::Prop_SerialNumber_String, m_sSerialNumber.c_str());
    vr::VRProperties()->SetInt32Property(m_ulPropertyContainer, vr::Prop_DeviceClass_Int32, vr::TrackedDeviceClass_GenericTracker);
    vr::VRProperties()->SetStringProperty(m_ulPropertyContainer, vr::Prop_RenderModelName_String, "{htc}/rendermodels/vr_tracker_vive_1_0");

    LoadConfig();

    WSADATA wsaData;
    if (WSAStartup(MAKEWORD(2, 2), &wsaData) != 0) {
        Log("Failed to initialize Winsock");
    }

    Log("Activating SLAM Tracker (starting background threads)...");

    m_bStopInitThread = false;
    m_initThread = std::thread(&CGenericTracker::InitThreadFunction, this);

    m_bStopUdpThread = false;
    m_udpThread = std::thread(&CGenericTracker::UdpThreadFunction, this);

    m_bStopPoseThread = false;
    m_poseThread = std::thread(&CGenericTracker::PoseThreadFunction, this);

    Log("CGenericTracker::Activate returning successfully.");
    return vr::VRInitError_None;
}

void CGenericTracker::InitThreadFunction()
{
    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    Log("InitThreadFunction started.");
    try
    {
        // Delay Xvisio SDK initialization by 12 seconds to avoid USB HID contention
        // with SteamVR's Lighthouse and HMD driver startup sequence.
        // If xvsdk scans HID devices while SteamVR is configuring them, it can crash.
        Log("Delaying Xvisio USB initialization by 12 seconds to prevent HID conflicts...");
        for (int i = 0; i < 120; ++i) {
            if (m_bStopInitThread) return;
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }

        Log("Attempting to get Xvisio devices in background thread...");
        auto devices = xv::getDevices(10.0, "", &m_bStopInitThread, xv::SlamStartMode::Normal, xv::DeviceSupport::ONLYUSB);

        if (m_bStopInitThread)
        {
            Log("InitThread aborted.");
            if (SUCCEEDED(hr)) CoUninitialize();
            return;
        }

        if (devices.empty())
        {
            Log("No Xvisio devices found.");
            if (SUCCEEDED(hr)) CoUninitialize();
            return;
        }

        m_pDevice = devices.begin()->second;
        if (!m_pDevice)
        {
            Log("Failed to get first device.");
            if (SUCCEEDED(hr)) CoUninitialize();
            return;
        }
        Log("Xvisio device found successfully: %s", m_pDevice->id().c_str());

        Log("Attempting to get SLAM interface...");
        auto pSlam = m_pDevice->slam();
        if (!pSlam)
        {
            Log("CRITICAL ERROR: The connected device does not support SLAM.");
            if (SUCCEEDED(hr)) CoUninitialize();
            return;
        }
        Log("SLAM interface acquired successfully.");

        Log("Attempting to start SLAM engine...");
        pSlam->start(xv::Slam::Mode::Mixed);

        Log("SUCCESS: Xvisio SLAM engine started in Mixed mode.");

        m_pSlam = pSlam; // Assign it now that it's started
        // The filtered Mixed-mode callback fires for every received IMU sample. Keep
        // the SDK callback extremely small: it only wakes our pose thread, which then
        // calls the supported getPose(prediction) API outside the SDK callback thread.
        // This avoids both the old 4 ms polling delay and the SDK crash seen when
        // Pose::prediction() was called directly from its internal callback thread.
        m_slamPoseCallbackId = m_pSlam->registerCallback([this](const xv::Pose& raw) {
            const double now = SteadyNowSeconds();
            m_lastCallbackTime.store(now, std::memory_order_relaxed);
            const double age = now - raw.hostTimestamp();
            if (std::isfinite(age) && age > -0.05 && age < 0.5)
                m_lastRawPoseAgeMs.store(age * 1000.0, std::memory_order_relaxed);
            m_poseWakeSequence.fetch_add(1, std::memory_order_release);
            m_poseWakeCv.notify_one();
        });
        Log("Registered filtered IMU-rate SLAM wake callback (id=%d).", m_slamPoseCallbackId);
        m_bSlamReady.store(true, std::memory_order_release); // Publish after m_pSlam is fully assigned

        // If a persisted CSLAM reference map exists, load it so this session's poses land
        // in the same map frame the one-time base-station calibration was done in.
        TryLoadCslamMap();

        // Continuous leveling reads the accelerometer. Subscribe only once SLAM has
        // settled so its proven startup sequence is untouched.
        for (int i = 0; i < 1200 && !m_bStopInitThread && !m_bSettled.load(); ++i)
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        if (!m_bStopInitThread && m_bSettled.load()) RegisterImuCallback();
    }
    catch (const std::exception& e)
    {
        Log("CRITICAL EXCEPTION in InitThread: %s", e.what());
    }
    catch (...)
    {
        Log("CRITICAL EXCEPTION (Unknown) in InitThread");
    }

    if (SUCCEEDED(hr)) {
        CoUninitialize();
    }
    Log("InitThreadFunction finished.");
}

void CGenericTracker::RegisterImuCallback()
{
    if (m_imuCallbackId >= 0 || !m_pDevice) return;
    try {
        if (auto imu = m_pDevice->imuSensor()) {
            m_imuCallbackId = imu->registerCallback([this](const xv::Imu& sample) {
                const auto& a = sample.accel;
                if (!std::isfinite(a[0]) || !std::isfinite(a[1]) || !std::isfinite(a[2])) return;
                std::lock_guard<std::mutex> lock(m_imuMutex);
                m_imuSum[0] += a[0]; m_imuSum[1] += a[1]; m_imuSum[2] += a[2];
                ++m_imuSamples;
            });
        }
    } catch (...) { m_imuCallbackId = -1; }
    Log("Leveling: accelerometer callback %s (id=%d).",
        m_imuCallbackId >= 0 ? "registered" : "unavailable", m_imuCallbackId);
}

void CGenericTracker::TryLoadCslamMap()
{
    bool useCslam;
    bool worldValid;
    {
        std::lock_guard<std::mutex> lock(m_configMutex);
        useCslam = m_config.use_cslam;
        worldValid = m_config.world_valid;
    }
    if (!useCslam) {
        Log("CSLAM map loading disabled by config (use_cslam=false).");
        return;
    }

    std::string mapPath = GetMapPath();
    if (mapPath.empty()) {
        Log("CSLAM: could not resolve map path (LOCALAPPDATA unset).");
        return;
    }

    auto fb = std::make_shared<std::filebuf>();
    if (!fb->open(mapPath, std::ios::in | std::ios::binary)) {
        Log("CSLAM: no saved map at %s -- running with a fresh per-session map. "
            "Run calibrate.py once (with base stations) to create one.", mapPath.c_str());
        return;
    }

    m_bCslamLoadAttempted = true;
    Log("CSLAM: loading reference map from %s ...", mapPath.c_str());

    // The filebuf must stay alive until the SDK's async done-callback fires; the
    // shared_ptr captured in the lambda guarantees that.
    bool ok = m_pSlam->loadMapAndSwitchToCslam(
        *fb,
        [this, fb](int status) {
            fb->close();
            Log("CSLAM: map load done-callback, status=%d", status);
            if (status >= 0) {
                // Loading alone is NOT enough: until the SDK actually relocalizes into the
                // reference map, poses are still in a fresh random session frame and the
                // persisted world transform must not be applied (doing so is what once
                // rotated/offset the whole world by the calibration C,t at startup).
                // Activation happens in the usage callback below once relocalized.
                Log("CSLAM: map loaded; waiting for relocalization before enabling world transform.");
            } else {
                Log("CSLAM: map load FAILED (status=%d); staying on per-session map.", status);
            }
        },
        [this](float pct) {
            // Fires periodically with the current reference-map usage. Treat the session
            // as relocalized -- and the persisted world transform as applicable -- only
            // once the SDK is genuinely matching against the reference map. Never
            // deactivate afterwards: once in the map frame, poses stay in the map frame.
            if (pct >= 0.2f && !m_bWorldTransformActive.load()) {
                m_bWorldTransformActive.store(true);
                Log("CSLAM: relocalized into reference map (usage %.0f%%); world transform enabled.", pct * 100.0f);
            }
            static int s_throttle = 0;
            if (s_throttle++ % 50 == 0) {
                Log("CSLAM: reference-map usage %.0f%%%s", pct * 100.0f,
                    m_bWorldTransformActive.load() ? "" : " (waiting to relocalize)");
            }
        });

    if (!ok) {
        Log("CSLAM: loadMapAndSwitchToCslam returned false; staying on per-session map.");
        fb->close();
    }
    else if (!worldValid) {
        Log("CSLAM: map loaded but no world calibration stored yet -- run calibrate.py.");
    }
}

void CGenericTracker::SaveCslamMap()
{
    if (!m_bSlamReady.load(std::memory_order_acquire)) {
        Log("SAVE_MAP ignored: SLAM not ready.");
        return;
    }
    std::string mapPath = GetMapPath();
    if (mapPath.empty()) {
        Log("SAVE_MAP failed: could not resolve map path.");
        return;
    }

    auto fb = std::make_shared<std::filebuf>();
    if (!fb->open(mapPath, std::ios::out | std::ios::binary | std::ios::trunc)) {
        Log("SAVE_MAP failed: cannot open %s for writing.", mapPath.c_str());
        return;
    }

    Log("SAVE_MAP: saving CSLAM reference map to %s ...", mapPath.c_str());
    bool ok = m_pSlam->saveMapAndSwitchToCslam(
        *fb,
        [this, fb](int status, int quality) {
            fb->close();
            Log("SAVE_MAP: done-callback status=%d quality=%d", status, quality);
            if (status >= 0) {
                // We just switched to CSLAM on the map we saved: current frame IS the
                // reference frame, so a world calibration from this session is valid.
                m_bWorldTransformActive.store(true);
            }
        },
        [](float) {});

    if (!ok) {
        Log("SAVE_MAP: saveMapAndSwitchToCslam returned false.");
        fb->close();
    }
}

void CGenericTracker::Deactivate()
{
    Log("CGenericTracker::Deactivate called");

    m_bStopInitThread = true;
    m_bStopUdpThread = true;
    m_bStopPoseThread = true;
    m_bSlamReady.store(false, std::memory_order_release); // Stop the pose thread touching m_pSlam before we reset it
    m_poseWakeCv.notify_all();

    if (m_udpSocket) {
        closesocket((SOCKET)m_udpSocket);
        m_udpSocket = 0;
    }

    if (m_initThread.joinable())
    {
        Log("Waiting for InitThread to finish...");
        m_initThread.join();
        Log("InitThread joined.");
    }

    if (m_poseThread.joinable())
    {
        m_poseThread.join();
        Log("Pose thread joined.");
    }

    if (m_udpThread.joinable())
    {
        Log("Waiting for UDP thread to finish...");
        m_udpThread.join();
        Log("UDP thread joined.");
    }

    if (m_imuCallbackId >= 0 && m_pDevice)
    {
        try {
            if (auto imu = m_pDevice->imuSensor()) imu->unregisterCallback(m_imuCallbackId);
        } catch (...) {}
        m_imuCallbackId = -1;
    }

    if (m_pSlam)
    {
        Log("Stopping SLAM engine...");
        try {
            if (m_slamPoseCallbackId >= 0) {
                m_pSlam->unregisterCallback(m_slamPoseCallbackId);
                m_slamPoseCallbackId = -1;
            }
            m_pSlam->stop();
        } catch (...) {
            Log("Exception while stopping SLAM engine.");
        }
        m_pSlam.reset();
        Log("SLAM engine stopped.");
    }
    if (m_pDevice)
    {
        m_pDevice.reset();
    }
    WSACleanup();

    m_unObjectId = vr::k_unTrackedDeviceIndexInvalid;
}

void CGenericTracker::EnterStandby() {}

void* CGenericTracker::GetComponent(const char* pchComponentNameAndVersion)
{
    return nullptr;
}

void CGenericTracker::DebugRequest(const char* pchRequest, char* pchResponseBuffer, uint32_t unResponseBufferSize)
{
    if (unResponseBufferSize > 0)
    {
        pchResponseBuffer[0] = '\0';
    }
}

vr::DriverPose_t CGenericTracker::GetPose()
{
    std::lock_guard<std::mutex> lock(m_poseMutex);
    return m_pose;
}

void CGenericTracker::RunFrame()
{
    // Pose acquisition and submission now live on the dedicated ~250 Hz pose thread
    // (PoseThreadFunction) so tracking is not quantized to SteamVR's RunFrame cadence.
}

void CGenericTracker::PoseThreadFunction()
{
#ifdef _WIN32
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_ABOVE_NORMAL);
#endif
    Log("Pose thread started (IMU callback-driven, polling fallback enabled).");
    uint64_t observedWakeSequence = m_poseWakeSequence.load(std::memory_order_acquire);
    while (!m_bStopPoseThread)
    {
        if (m_bSlamReady.load(std::memory_order_acquire))
        {
            // Wake on each filtered IMU-rate SLAM callback. The short timeout is a
            // fallback for firmware that starts SLAM successfully but does not emit
            // Stream<Pose> callbacks; it is not the normal pacing path.
            {
                std::unique_lock<std::mutex> lock(m_poseWakeMutex);
                m_poseWakeCv.wait_for(lock, std::chrono::milliseconds(5), [&] {
                    return m_bStopPoseThread.load() ||
                        m_poseWakeSequence.load(std::memory_order_acquire) != observedWakeSequence;
                });
                observedWakeSequence = m_poseWakeSequence.load(std::memory_order_acquire);
            }
            if (m_bStopPoseThread.load()) break;
            try {
                PoseTick();
            } catch (const std::exception& e) {
                Log("EXCEPTION in PoseTick: %s", e.what());
            } catch (...) {
                Log("EXCEPTION (unknown) in PoseTick");
            }
        }
        else
        {
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
    }
    Log("Pose thread stopped.");
}

void CGenericTracker::PoseTick()
{
    const double requestStart = SteadyNowSeconds();
    const double callbackTime = m_lastCallbackTime.load(std::memory_order_relaxed);

    // Optional SDK forward prediction, only after warmup: the SDK predictor is known to
    // crash if invoked while the IMU state is still uninitialized.
    double prediction = 0.0;
    {
        std::lock_guard<std::mutex> lock(m_configMutex);
        prediction = m_config.prediction_s;
    }
    const bool warmedUp = (m_firstValidTime > 0.0) && (requestStart - m_firstValidTime > 5.0);
    if (!warmedUp) prediction = 0.0;
    prediction = std::max(0.0, std::min(prediction, 0.05));

    xv::Pose slam_pose;
    bool ok = false;
    double appliedPrediction = prediction;
    double poseRequestStart = requestStart;
    try { ok = m_pSlam->getPose(slam_pose, prediction); } catch (...) { ok = false; }
    if (!ok && prediction > 0.0) {
        appliedPrediction = 0.0;
        poseRequestStart = SteadyNowSeconds();
        try { ok = m_pSlam->getPose(slam_pose, 0.0); } catch (...) { ok = false; }
    }

    const double requestEnd = SteadyNowSeconds();
    m_lastGetPoseMs.store((requestEnd - requestStart) * 1000.0);
    m_lastAppliedPredictionMs.store(ok ? appliedPrediction * 1000.0 : 0.0);

    if (!ok)
    {
        std::lock_guard<std::mutex> lock(m_poseMutex);
        m_pose.poseIsValid = false;
        m_pose.result = vr::TrackingResult_Calibrating_InProgress;
        if (m_unObjectId != vr::k_unTrackedDeviceIndexInvalid)
            vr::VRServerDriverHost()->TrackedDevicePoseUpdated(m_unObjectId, m_pose, sizeof(vr::DriverPose_t));
        if (m_calibrationTracker) m_calibrationTracker->PublishStatus(m_pose);
        return;
    }

    // The SDK contract says getPose(p, dt) returns the pose at "now + dt".
    // Use the call midpoint as the best estimate of that internal "now". The
    // physical sensor timestamp remains useful for diagnostics, but assigning it
    // to OpenVR's poseTimeOffset would make SteamVR extrapolate sensor latency that
    // the SDK has already compensated, producing double prediction.
    const double sdkPoseTime = 0.5 * (poseRequestStart + requestEnd) + appliedPrediction;

    HmdSlamData d;
    auto translation = slam_pose.translation();
    d.x = translation[0]; d.y = translation[1]; d.z = translation[2];

    auto rotation = slam_pose.quaternion(); // [qx, qy, qz, qw]
    d.qx = rotation[0]; d.qy = rotation[1]; d.qz = rotation[2]; d.qw = rotation[3];

    auto linVel = slam_pose.linearVelocity();
    d.vx = linVel[0]; d.vy = linVel[1]; d.vz = linVel[2];
    if (!std::isfinite(d.vx) || !std::isfinite(d.vy) || !std::isfinite(d.vz)) {
        d.vx = d.vy = d.vz = 0.0;
    }

    auto angVel = slam_pose.angularVelocity();
    d.wx = angVel[0]; d.wy = angVel[1]; d.wz = angVel[2];
    if (!std::isfinite(d.wx) || !std::isfinite(d.wy) || !std::isfinite(d.wz)) {
        d.wx = d.wy = d.wz = 0.0;
    }

    d.confidence = slam_pose.confidence();
    d.hostTimestamp = slam_pose.hostTimestamp();

    if (std::isfinite(d.hostTimestamp) && d.hostTimestamp > 0.0)
    {
        const double sampleAge = requestEnd - d.hostTimestamp;
        if (sampleAge >= -0.05 && sampleAge <= 0.5)
            m_lastSampleAgeMs.store(sampleAge * 1000.0);
    }

    m_lastConfidence.store(d.confidence);

    // --- Confidence gating -------------------------------------------------------
    // confidence() is documented as [0,1] with 0 == lost, but some firmwares never
    // populate it. Only trust "0 == lost" after we have seen a nonzero value once.
    static thread_local bool s_confidenceMeaningful = false;
    if (d.confidence > 0.0) s_confidenceMeaningful = true;
    const bool lost = s_confidenceMeaningful && d.confidence <= 0.0;

    if (m_firstValidTime <= 0.0 && !lost) m_firstValidTime = requestEnd;

    // --- Startup settle ----------------------------------------------------------
    // Don't publish (or capture the floor) until SLAM has produced ~1.5 s of
    // uninterrupted confident poses: the first frames after start can have an
    // unconverged gravity estimate, which is what baked in the "starts tilted /
    // wrong height" poses before.
    constexpr int kSettleFrames = 375; // ~1.5 s at 250 Hz
    if (!m_bSettled.load())
    {
        if (lost) {
            m_settleCounter.store(0);
        } else if (m_settleCounter.fetch_add(1) + 1 >= kSettleFrames) {
            m_bSettled.store(true);
            Log("Startup settle complete (%.1f s of confident tracking).", kSettleFrames * 0.004);
        }

        std::lock_guard<std::mutex> lock(m_poseMutex);
        m_pose.deviceIsConnected = true;
        m_pose.poseIsValid = false;
        m_pose.result = vr::TrackingResult_Calibrating_InProgress;
        if (m_unObjectId != vr::k_unTrackedDeviceIndexInvalid)
            vr::VRServerDriverHost()->TrackedDevicePoseUpdated(m_unObjectId, m_pose, sizeof(vr::DriverPose_t));
        if (m_calibrationTracker) m_calibrationTracker->PublishStatus(m_pose);
        return;
    }

    if (lost)
    {
        std::lock_guard<std::mutex> lock(m_poseMutex);
        m_pose.deviceIsConnected = true;
        m_pose.poseIsValid = false;
        m_pose.result = vr::TrackingResult_Running_OutOfRange;
        if (m_unObjectId != vr::k_unTrackedDeviceIndexInvalid)
            vr::VRServerDriverHost()->TrackedDevicePoseUpdated(m_unObjectId, m_pose, sizeof(vr::DriverPose_t));
        if (m_calibrationTracker) m_calibrationTracker->PublishStatus(m_pose);
        return;
    }

    BuildAndSubmitPose(d, sdkPoseTime);
    const double submittedAt = SteadyNowSeconds();
    m_lastPoseTickMs.store((submittedAt - requestStart) * 1000.0);
    if (callbackTime > 0.0 && requestStart - callbackTime >= 0.0 && requestStart - callbackTime < .05)
        m_lastCallbackToSubmitMs.store((submittedAt - callbackTime) * 1000.0);
    else
        m_lastCallbackToSubmitMs.store(-1.0);

    // UDP streaming for hand-eye calibration
    if (m_bStreamCalibrationData.load() && m_udpSocket != 0)
    {
        StreamCalibrationPacket(slam_pose);
    }
}

void CGenericTracker::BuildAndSubmitPose(const HmdSlamData& slamData, double sdkPoseTimeSeconds)
{
    TrackerConfig cfg;
    {
        std::lock_guard<std::mutex> lock(m_configMutex);
        cfg = m_config;
    }

    // Xvisio VSLAM uses a right-handed camera-style coordinate system:
    // +X = Right, +Y = Down, +Z = Forward
    // OpenVR expects: +X = Right, +Y = Up, +Z = Backward
    //
    // The coordinate map is: X_ovr = X_xv, Y_ovr = -Y_xv, Z_ovr = -Z_xv.
    // This is a proper 180-degree rotation around X (not a reflection).
    // Changing basis maps quaternion (w,x,y,z) to (w,x,-y,-z).
    // This exact mapping is duplicated in calibrate.py -- keep them in sync.
    vr::HmdQuaternion_t q_dev = NormalizeQuaternion({ slamData.qw, slamData.qx, -slamData.qy, -slamData.qz });
    vr::HmdVector3d_t   p_dev = { slamData.x, -slamData.y, -slamData.z };
    vr::HmdVector3d_t   v_dev = { slamData.vx, -slamData.vy, -slamData.vz };
    vr::HmdVector3d_t   w_dev = { slamData.wx, -slamData.wy, -slamData.wz };
    const angular_velocity::Quat aq{q_dev.w, q_dev.x, q_dev.y, q_dev.z};
    const angular_velocity::Vec3 aw{w_dev.v[0], w_dev.v[1], w_dev.v[2]};
    const auto frame = m_angularSelector.update(sdkPoseTimeSeconds, aq, aw);
    m_angularFrame.store(static_cast<int>(frame));
    const auto worldOmega = m_angularSelector.world_velocity(aq, aw);
    vr::HmdVector3d_t w_world = {worldOmega.x, worldOmega.y, worldOmega.z};
    // Linear velocity's frame is just as undocumented; send it only once validated.
    const auto linearFrame = m_linearSelector.update(sdkPoseTimeSeconds, aq,
        {p_dev.v[0], p_dev.v[1], p_dev.v[2]}, {v_dev.v[0], v_dev.v[1], v_dev.v[2]});
    m_linearFrame.store(static_cast<int>(linearFrame));
    const auto worldLinear = m_linearSelector.world_velocity(aq, {v_dev.v[0], v_dev.v[1], v_dev.v[2]});
    v_dev = {worldLinear.x, worldLinear.y, worldLinear.z};

    // ---- Two distinct rotation corrections; do not conflate them ----
    //
    // 1) Mount rotation X (BODY frame, right-multiply): the XR50 sits tilted on the
    //    Beyond (pitched down on the forehead), a constant rigid rotation between the
    //    camera frame and the HMD frame. Calibratable once, valid forever:
    //        q_head = q_dev * X
    //
    // 2) World transform C,t (WORLD frame, left-multiply): the SLAM map's yaw origin is
    //    arbitrary per session, so a constant world yaw can only be applied when the map
    //    itself persists (CSLAM reference map loaded). Otherwise SteamVR "recenter"
    //    absorbs it. A body-frame offset can NEVER fix world yaw at all headings --
    //    that is what produced the bogus ~172-degree offset from the old calibration.
    vr::HmdQuaternion_t X = NormalizeQuaternion({ cfg.mount_qw, cfg.mount_qx, cfg.mount_qy, cfg.mount_qz });
    vr::HmdQuaternion_t q_head = NormalizeQuaternion(MultiplyQuaternions(q_dev, X));

    // Lever arm: L is the device->head vector in the device body frame.
    // Head position = device position + R(q_dev) * L.
    vr::HmdVector3d_t L = { cfg.lever_x, cfg.lever_y, cfg.lever_z };
    vr::HmdVector3d_t r = RotateVectorByQuaternion(L, q_dev); // world-frame lever arm
    vr::HmdVector3d_t p_head = { p_dev.v[0] + r.v[0], p_dev.v[1] + r.v[1], p_dev.v[2] + r.v[2] };

    // Rigid-body velocity transfer: the head point moves with v + w x r.
    const auto headVelocity = angular_velocity::point_velocity(
        {v_dev.v[0], v_dev.v[1], v_dev.v[2]}, worldOmega, {r.v[0], r.v[1], r.v[2]});
    vr::HmdVector3d_t v_head = {headVelocity.x, headVelocity.y, headVelocity.z};

    vr::DriverPose_t pose = { 0 };
    pose.qWorldFromDriverRotation = { 1.0, 0.0, 0.0, 0.0 };
    pose.qDriverFromHeadRotation = { 1.0, 0.0, 0.0, 0.0 };
    pose.deviceIsConnected = true;
    pose.poseIsValid = true;
    pose.result = vr::TrackingResult_Running_OK;
    pose.willDriftInYaw = false;
    pose.shouldApplyHeadModel = false;

    const bool applyWorld = cfg.world_valid && m_bWorldTransformActive.load();
    if (applyWorld)
    {
        // Full calibrated mapping into the OpenVR universe (base-station calibration,
        // valid because the CSLAM reference map pins the SLAM frame across sessions).
        vr::HmdQuaternion_t C = NormalizeQuaternion({ cfg.world_qw, cfg.world_qx, cfg.world_qy, cfg.world_qz });
        pose.qRotation = NormalizeQuaternion(MultiplyQuaternions(C, q_head));

        vr::HmdVector3d_t p = RotateVectorByQuaternion(p_head, C);
        pose.vecPosition[0] = p.v[0] + cfg.world_x;
        pose.vecPosition[1] = p.v[1] + cfg.world_y;
        pose.vecPosition[2] = p.v[2] + cfg.world_z;

        vr::HmdVector3d_t v = RotateVectorByQuaternion(v_head, C);
        vr::HmdVector3d_t w = RotateVectorByQuaternion(w_world, C);
        pose.vecVelocity[0] = v.v[0]; pose.vecVelocity[1] = v.v[1]; pose.vecVelocity[2] = v.v[2];
        if (cfg.use_angular_velocity) {
            pose.vecAngularVelocity[0] = w.v[0]; pose.vecAngularVelocity[1] = w.v[1]; pose.vecAngularVelocity[2] = w.v[2];
        }
    }
    else
    {
        // Uncalibrated session frame: yaw is arbitrary (use SteamVR recenter), height is
        // referenced to the pose captured at settle time plus the configured head_height.
        pose.qRotation = q_head;

        if (!m_bFloorCaptured.load())
        {
            m_floorY0.store(p_head.v[1]);
            m_heightAdd.store(cfg.head_height);
            m_bFloorCaptured.store(true);
            Log("Floor reference captured at settle: raw Y=%.3f, assumed height above floor=%.3f",
                p_head.v[1], cfg.head_height);
        }

        pose.vecPosition[0] = p_head.v[0];
        pose.vecPosition[1] = p_head.v[1] - m_floorY0.load() + m_heightAdd.load();
        pose.vecPosition[2] = p_head.v[2];

        pose.vecVelocity[0] = v_head.v[0]; pose.vecVelocity[1] = v_head.v[1]; pose.vecVelocity[2] = v_head.v[2];
        if (cfg.use_angular_velocity) {
            pose.vecAngularVelocity[0] = w_world.v[0]; pose.vecAngularVelocity[1] = w_world.v[1]; pose.vecAngularVelocity[2] = w_world.v[2];
        }
    }

    // ---- Gravity leveling (measured during floor calibration) ----
    // Drain the accelerometer every tick so stale samples never reach a capture.
    int imuSamples;
    double accel[3];
    {
        std::lock_guard<std::mutex> lock(m_imuMutex);
        imuSamples = m_imuSamples;
        for (int axis = 0; axis < 3; ++axis) { accel[axis] = m_imuSum[axis]; m_imuSum[axis] = 0.0; }
        m_imuSamples = 0;
    }
    const floor_level::Vec3 unleveled{pose.vecPosition[0], pose.vecPosition[1], pose.vecPosition[2]};
    floor_level::Level level;
    {
        std::lock_guard<std::mutex> lock(m_levelMutex);
        level = m_level;
    }
    const vr::HmdQuaternion_t levelQ{level.q.w, level.q.x, level.q.y, level.q.z};
    {
        const auto p = level.apply({pose.vecPosition[0], pose.vecPosition[1], pose.vecPosition[2]});
        pose.vecPosition[0] = p.x; pose.vecPosition[1] = p.y; pose.vecPosition[2] = p.z;
        pose.qRotation = NormalizeQuaternion(MultiplyQuaternions(levelQ, pose.qRotation));
        const auto v = RotateVectorByQuaternion({pose.vecVelocity[0], pose.vecVelocity[1], pose.vecVelocity[2]}, levelQ);
        const auto w = RotateVectorByQuaternion({pose.vecAngularVelocity[0], pose.vecAngularVelocity[1], pose.vecAngularVelocity[2]}, levelQ);
        for (int axis = 0; axis < 3; ++axis) {
            pose.vecVelocity[axis] = v.v[axis];
            pose.vecAngularVelocity[axis] = w.v[axis];
        }
    }

    pose.vecPosition[1] += m_floorAdjustY.load();
    if (m_floorScanStatus.load() == 1) {
        const double now = SteadyNowSeconds();
        if (!m_groundCaptureActive) {
            m_groundCaptureActive = true;
            m_groundCaptureStarted = now;
            m_groundLastSensorTimestamp = 0.0;
            m_groundSamples = 0;
            m_groundSum = {0, 0, 0};
            m_groundUpSum = {0, 0, 0};
            m_groundImuSamples = 0;
            m_groundMinRotationDot = 1.0;
            m_groundStartRotation = pose.qRotation;
        }
        if (imuSamples > 0) {
            // Mean specific force points up; map Xvisio axes like the pose (x, -y, -z) and
            // take it into the unleveled world, so a new capture replaces the old level.
            auto deviceWorld = q_dev;
            if (applyWorld)
                deviceWorld = MultiplyQuaternions(
                    NormalizeQuaternion({cfg.world_qw, cfg.world_qx, cfg.world_qy, cfg.world_qz}), q_dev);
            const auto up = RotateVectorByQuaternion({{accel[0], -accel[1], -accel[2]}}, deviceWorld);
            m_groundUpSum = {m_groundUpSum.x + up.v[0], m_groundUpSum.y + up.v[1], m_groundUpSum.z + up.v[2]};
            m_groundImuSamples += imuSamples;
        }
        // Repeated calls to getPose must not count as new tracking samples.
        if (std::isfinite(slamData.hostTimestamp) &&
            slamData.hostTimestamp > m_groundLastSensorTimestamp + 0.0005 &&
            std::isfinite(pose.vecPosition[0]) && std::isfinite(pose.vecPosition[1]) &&
            std::isfinite(pose.vecPosition[2])) {
            m_groundLastSensorTimestamp = slamData.hostTimestamp;
            const vr::HmdVector3d_t p{{pose.vecPosition[0], pose.vecPosition[1], pose.vecPosition[2]}};
            if (m_groundSamples == 0) {
                m_groundMinPosition = m_groundMaxPosition = p;
            } else {
                for (int axis = 0; axis < 3; ++axis) {
                    m_groundMinPosition.v[axis] = std::min(m_groundMinPosition.v[axis], p.v[axis]);
                    m_groundMaxPosition.v[axis] = std::max(m_groundMaxPosition.v[axis], p.v[axis]);
                }
            }
            const auto& a = m_groundStartRotation;
            const auto& b = pose.qRotation;
            const double dot = std::abs(a.w*b.w + a.x*b.x + a.y*b.y + a.z*b.z);
            m_groundMinRotationDot = std::min(m_groundMinRotationDot, dot);
            m_groundSum = {m_groundSum.x + unleveled.x, m_groundSum.y + unleveled.y, m_groundSum.z + unleveled.z};
            ++m_groundSamples;
        }
        if (now - m_groundCaptureStarted >= 2.0) {
            const bool stable = m_groundSamples >= 30 &&
                m_groundMaxPosition.v[0] - m_groundMinPosition.v[0] <= 0.05 &&
                m_groundMaxPosition.v[1] - m_groundMinPosition.v[1] <= 0.025 &&
                m_groundMaxPosition.v[2] - m_groundMinPosition.v[2] <= 0.05 &&
                m_groundMinRotationDot >= std::cos(8.0 * 3.14159265358979323846 / 360.0);
            if (stable) {
                const floor_level::Vec3 rest{m_groundSum.x / m_groundSamples, m_groundSum.y / m_groundSamples,
                                             m_groundSum.z / m_groundSamples};
                // The floor is level by definition, so the resting accelerometer gives the
                // world's tilt directly. Rotate about the resting point so it stays put.
                const int n = m_groundImuSamples;
                const floor_level::Vec3 up{n ? m_groundUpSum.x / n : 0, n ? m_groundUpSum.y / n : 0, n ? m_groundUpSum.z / n : 0};
                const double g = std::sqrt(angular_velocity::norm2(up));
                floor_level::Quat q;
                double tilt = 0.0;
                if (n >= 200 && g > 8.5 && g < 11.0 && floor_level::rotation_to_up(up, 10.0, q, tilt)) {
                    level = floor_level::compose(floor_level::Level(), q, rest);
                    {
                        std::lock_guard<std::mutex> lock(m_levelMutex);
                        m_level = level;
                    }
                    m_levelReady.store(true);
                    m_levelDegrees.store(tilt);
                    m_accelNormReport.store(g);
                    Log("Floor leveled from %d accelerometer samples: tilt %.2f deg, |a|=%.3f.", n, tilt, g);
                } else {
                    Log("Floor leveling skipped (%d accelerometer samples, |a|=%.3f, tilt %.2f deg); keeping %.2f deg.",
                        n, g, tilt, m_levelDegrees.load());
                }
                constexpr double kDeviceHeightAboveFloor = 0.01;
                const double adjust = kDeviceHeightAboveFloor - level.apply(rest).y;
                m_floorAdjustY.store(adjust);
                m_floorScanHeight.store(kDeviceHeightAboveFloor);
                m_floorScanStatus.store(2);
                Log("Floor calibrated from %d stable pose samples over 2 seconds; vertical offset %.3f m.",
                    m_groundSamples, adjust);
            } else {
                m_floorScanStatus.store(6);
                Log("Floor calibration rejected: %d distinct poses or excessive movement during 2 seconds.",
                    m_groundSamples);
            }
            m_groundCaptureActive = false;
        }
    } else {
        m_groundCaptureActive = false;
    }

    // Accelerations deliberately zeroed: IMU accel is too noisy for SteamVR's
    // extrapolator and caused jitter amplification.
    // (vecAcceleration / vecAngularAcceleration already zero from = {0}.)

    // ---- Latency accounting -----------------------------------------------------
    // getPose(p, prediction) returns a pose for SDK-now + prediction. OpenVR wants
    // the time represented by this pose relative to this PoseUpdated call. A manual
    // positive time_offset means "have SteamVR extrapolate this much farther", hence
    // it is subtracted. Clamp bad settings so a typo cannot create a wild pose.
    const double manualPrediction = std::max(-0.05, std::min(cfg.time_offset, 0.10));
    pose.poseTimeOffset = sdkPoseTimeSeconds - SteadyNowSeconds() - manualPrediction;
    pose.poseTimeOffset = std::max(-0.10, std::min(pose.poseTimeOffset, 0.05));
    m_lastPoseOffsetMs.store(pose.poseTimeOffset * 1000.0);

    // Angular velocity is world-space up to here. Optionally express it in each pose's
    // own frame instead (see TrackerConfig::angular_velocity_local).
    const vr::HmdVector3d_t worldAngular{{pose.vecAngularVelocity[0], pose.vecAngularVelocity[1], pose.vecAngularVelocity[2]}};
    auto toLocal = [](const vr::HmdVector3d_t& w, const vr::HmdQuaternion_t& q) {
        return RotateVectorByQuaternion(w, {q.w, -q.x, -q.y, -q.z});
    };
    if (cfg.angular_velocity_local) {
        const auto local = toLocal(worldAngular, pose.qRotation);
        for (int axis = 0; axis < 3; ++axis) pose.vecAngularVelocity[axis] = local.v[axis];
    }

    if (m_calibrationTracker) {
        // Same room transform and final floor correction as the HMD, with the
        // mount/lever removed. Publishing an unshifted map pose here made Space
        // Calibrator align SLAM-CAL while leaving the HMD metres above it.
        auto sensorRotation = q_dev;
        auto worldLever = r;
        vr::HmdVector3d_t leverVelocity = {
            v_head.v[0] - v_dev.v[0], v_head.v[1] - v_dev.v[1], v_head.v[2] - v_dev.v[2]};
        if (applyWorld) {
            const auto C = NormalizeQuaternion({cfg.world_qw, cfg.world_qx, cfg.world_qy, cfg.world_qz});
            sensorRotation = NormalizeQuaternion(MultiplyQuaternions(C, q_dev));
            worldLever = RotateVectorByQuaternion(worldLever, C);
            leverVelocity = RotateVectorByQuaternion(leverVelocity, C);
        }
        // The head pose is already leveled; level the sensor's own rotation and lever too.
        sensorRotation = NormalizeQuaternion(MultiplyQuaternions(levelQ, sensorRotation));
        worldLever = RotateVectorByQuaternion(worldLever, levelQ);
        leverVelocity = RotateVectorByQuaternion(leverVelocity, levelQ);
        auto sensor = CCalibrationTracker::SensorPose(pose, sensorRotation, worldLever, leverVelocity);
        if (cfg.angular_velocity_local) {
            const auto local = toLocal(worldAngular, sensor.qRotation);
            for (int axis = 0; axis < 3; ++axis) sensor.vecAngularVelocity[axis] = local.v[axis];
        }
        sensor.poseTimeOffset = std::max(-0.10, std::min(sdkPoseTimeSeconds - SteadyNowSeconds(), 0.05));
        m_calibrationTracker->Publish(sensor);
    }

    {
        std::lock_guard<std::mutex> lock(m_poseMutex);
        m_pose = pose;
    }
    if (m_unObjectId != vr::k_unTrackedDeviceIndexInvalid)
    {
        vr::VRServerDriverHost()->TrackedDevicePoseUpdated(m_unObjectId, pose, sizeof(vr::DriverPose_t));
    }
}

void CGenericTracker::StreamCalibrationPacket(const xv::Pose& slam_pose)
{
    vr::TrackedDevicePose_t poses[vr::k_unMaxTrackedDeviceCount];
    vr::VRServerDriverHost()->GetRawTrackedDevicePoses(0, poses, vr::k_unMaxTrackedDeviceCount);
    vr::TrackedDevicePose_t hmdPose = poses[vr::k_unTrackedDeviceIndex_Hmd];

    if (!hmdPose.bPoseIsValid) return;

    char packet[512];
    auto st = slam_pose.translation();
    auto sq = slam_pose.quaternion(); // [qx, qy, qz, qw] -- matches calibrate.py
    auto hm = hmdPose.mDeviceToAbsoluteTracking.m;
    snprintf(packet, sizeof(packet), "DATA %f %f %f %f %f %f %f %f %f %f %f %f %f %f %f %f %f %f %f",
        hm[0][3], hm[1][3], hm[2][3],
        hm[0][0], hm[0][1], hm[0][2],
        hm[1][0], hm[1][1], hm[1][2],
        hm[2][0], hm[2][1], hm[2][2],
        st[0], st[1], st[2],
        sq[0], sq[1], sq[2], sq[3]);

    sockaddr_in clientAddr;
    clientAddr.sin_family = AF_INET;
    clientAddr.sin_port = htons(m_clientPort);
    inet_pton(AF_INET, m_clientIp, &clientAddr.sin_addr);

    sendto((SOCKET)m_udpSocket, packet, (int)strlen(packet), 0, (sockaddr*)&clientAddr, sizeof(clientAddr));
}

// Helper function to rotate a vector by a quaternion
vr::HmdVector3d_t CGenericTracker::RotateVectorByQuaternion(const vr::HmdVector3d_t& vec, const vr::HmdQuaternion_t& quat)
{
    // Convert vector to quaternion for rotation
    vr::HmdQuaternion_t vec_quat = { 0.0, vec.v[0], vec.v[1], vec.v[2] };

    // Create conjugate of rotation quaternion
    vr::HmdQuaternion_t quat_conj = { quat.w, -quat.x, -quat.y, -quat.z };

    // Rotate: result = quat * vec_quat * quat_conjugate
    vr::HmdQuaternion_t temp = MultiplyQuaternions(quat, vec_quat);
    vr::HmdQuaternion_t result_quat = MultiplyQuaternions(temp, quat_conj);

    // Extract the vector part
    vr::HmdVector3d_t result;
    result.v[0] = result_quat.x;
    result.v[1] = result_quat.y;
    result.v[2] = result_quat.z;

    return result;
}

vr::HmdQuaternion_t CGenericTracker::NormalizeQuaternion(vr::HmdQuaternion_t q)
{
    double len = sqrt(q.w * q.w + q.x * q.x + q.y * q.y + q.z * q.z);
    if (len < 1e-6)
    {
        return { 1.0, 0.0, 0.0, 0.0 };
    }

    vr::HmdQuaternion_t result;
    result.w = q.w / len;
    result.x = q.x / len;
    result.y = q.y / len;
    result.z = q.z / len;
    return result;
}

vr::HmdQuaternion_t CGenericTracker::MultiplyQuaternions(vr::HmdQuaternion_t a, vr::HmdQuaternion_t b)
{
    // Standard Hamilton product a (x) b: composing rotations, b is applied first, then a.
    vr::HmdQuaternion_t result;
    result.w = a.w * b.w - a.x * b.x - a.y * b.y - a.z * b.z;
    result.x = a.w * b.x + a.x * b.w + a.y * b.z - a.z * b.y;
    result.y = a.w * b.y - a.x * b.z + a.y * b.w + a.z * b.x;
    result.z = a.w * b.z + a.x * b.y - a.y * b.x + a.z * b.w;
    return result;
}

void CGenericTracker::LoadConfig()
{
    TrackerConfig cfg; // defaults from the struct initializers
    vr::EVRSettingsError err;

    auto getF = [&](const char* key, double& out) {
        float v = vr::VRSettings()->GetFloat("BSB-SLAM", key, &err);
        if (err == vr::VRSettingsError_None) out = v;
    };
    auto getB = [&](const char* key, bool& out) {
        bool v = vr::VRSettings()->GetBool("BSB-SLAM", key, &err);
        if (err == vr::VRSettingsError_None) out = v;
    };
    auto getI = [&](const char* key, int& out) {
        int32_t v = vr::VRSettings()->GetInt32("BSB-SLAM", key, &err);
        if (err == vr::VRSettingsError_None) out = static_cast<int>(v);
    };

    getI("calibration_version", cfg.calibration_version);

    getF("offset_x", cfg.lever_x);
    getF("offset_y", cfg.lever_y);
    getF("offset_z", cfg.lever_z);

    getF("offset_qw", cfg.mount_qw);
    getF("offset_qx", cfg.mount_qx);
    getF("offset_qy", cfg.mount_qy);
    getF("offset_qz", cfg.mount_qz);

    getF("world_qw", cfg.world_qw);
    getF("world_qx", cfg.world_qx);
    getF("world_qy", cfg.world_qy);
    getF("world_qz", cfg.world_qz);
    getF("world_x", cfg.world_x);
    getF("world_y", cfg.world_y);
    getF("world_z", cfg.world_z);
    getB("world_valid", cfg.world_valid);

    getF("time_offset", cfg.time_offset);
    getF("prediction_s", cfg.prediction_s);
    getB("use_angular_velocity", cfg.use_angular_velocity);
    getB("angular_velocity_local", cfg.angular_velocity_local);
    getF("head_height", cfg.head_height);
    getB("use_cslam", cfg.use_cslam);

    if (cfg.calibration_version != kCalibrationVersion)
    {
        // A calibration absorbs the raw coordinate basis. Applying values solved
        // for the old Y-up/X-left mapping to the corrected X-right/Y-down mapping
        // can rotate motion into the wrong axes, so fall back to the known physical
        // mount until calibrate.py stores a calibration for this mapping version.
        const TrackerConfig hardwareDefaults;
        cfg.lever_x = hardwareDefaults.lever_x;
        cfg.lever_y = hardwareDefaults.lever_y;
        cfg.lever_z = hardwareDefaults.lever_z;
        cfg.mount_qw = hardwareDefaults.mount_qw;
        cfg.mount_qx = hardwareDefaults.mount_qx;
        cfg.mount_qy = hardwareDefaults.mount_qy;
        cfg.mount_qz = hardwareDefaults.mount_qz;
        cfg.world_qw = 1.0;
        cfg.world_qx = cfg.world_qy = cfg.world_qz = 0.0;
        cfg.world_x = cfg.world_y = cfg.world_z = 0.0;
        cfg.world_valid = false;
        Log("Ignoring stale pose calibration version %d (driver expects %d); using gravity-correct hardware mount default.",
            cfg.calibration_version, kCalibrationVersion);
    }

    {
        std::lock_guard<std::mutex> lock(m_configMutex);
        m_config = cfg;
    }

    Log("Loaded Config: LeverArm=(%.3f,%.3f,%.3f) Mount=(%.3f,%.3f,%.3f,%.3f) "
        "World=(%.3f,%.3f,%.3f,%.3f | %.3f,%.3f,%.3f valid=%d) "
        "TimeOffset=%.3f Prediction=%.3f AngularVelocity=%d AngularLocal=%d HeadHeight=%.2f UseCslam=%d",
        cfg.lever_x, cfg.lever_y, cfg.lever_z,
        cfg.mount_qw, cfg.mount_qx, cfg.mount_qy, cfg.mount_qz,
        cfg.world_qw, cfg.world_qx, cfg.world_qy, cfg.world_qz,
        cfg.world_x, cfg.world_y, cfg.world_z, (int)cfg.world_valid,
        cfg.time_offset, cfg.prediction_s, (int)cfg.use_angular_velocity,
        (int)cfg.angular_velocity_local, cfg.head_height, (int)cfg.use_cslam);
}

static std::string CameraCalibrationJson(const std::vector<xv::Calibration>& cameras)
{
    std::ostringstream out;
    out << std::setprecision(17)
        << "{\"schema\":\"beyond-slam/xvisio-fisheye-calibration/v1\","
           "\"frame\":\"Xvisio IMU optical frame\",\"translation_unit\":\"m\",\"cameras\":[";
    for (size_t i = 0; i < cameras.size(); ++i) {
        if (i) out << ',';
        const auto& c = cameras[i];
        const auto t = c.pose.translation();
        const auto r = c.pose.rotation();
        out << "{\"index\":" << i << ",\"name\":\"" << (i == 0 ? "left" : i == 1 ? "right" : "other")
            << "\",\"camera_to_imu\":{\"translation_xyz\":[" << t[0] << ',' << t[1] << ',' << t[2]
            << "],\"rotation_row_major_3x3\":[";
        for (size_t j = 0; j < 9; ++j) { if (j) out << ','; out << r[j]; }
        out << "]},\"unified_models\":[";
        for (size_t j = 0; j < c.ucm.size(); ++j) {
            if (j) out << ',';
            const auto& m = c.ucm[j];
            out << "{\"width\":" << m.w << ",\"height\":" << m.h
                << ",\"fx\":" << m.fx << ",\"fy\":" << m.fy
                << ",\"cx\":" << m.u0 << ",\"cy\":" << m.v0
                << ",\"xi\":" << m.xi << '}';
        }
        out << "],\"polynomial_models\":[";
        for (size_t j = 0; j < c.pdcm.size(); ++j) {
            if (j) out << ',';
            const auto& m = c.pdcm[j];
            out << "{\"width\":" << m.w << ",\"height\":" << m.h
                << ",\"fx\":" << m.fx << ",\"fy\":" << m.fy
                << ",\"cx\":" << m.u0 << ",\"cy\":" << m.v0 << ",\"distortion\":[";
            for (size_t k = 0; k < m.distor.size(); ++k) { if (k) out << ','; out << m.distor[k]; }
            out << "]}";
        }
        out << "]}";
    }
    out << "]}";
    return out.str();
}

void CGenericTracker::UdpThreadFunction()
{
    Log("UDP IPC Thread started on port 8421");
    m_udpSocket = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (m_udpSocket == INVALID_SOCKET) {
        Log("UDP socket creation failed");
        return;
    }

    // Set non-blocking mode
    u_long mode = 1;
    ioctlsocket((SOCKET)m_udpSocket, FIONBIO, &mode);

    sockaddr_in serverAddr;
    serverAddr.sin_family = AF_INET;
    serverAddr.sin_port = htons(8421);
    // Configuration commands change persistent tracking settings. Only local tools
    // should be able to reach this unauthenticated control socket.
    serverAddr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);

    if (bind((SOCKET)m_udpSocket, (sockaddr*)&serverAddr, sizeof(serverAddr)) == SOCKET_ERROR) {
        Log("UDP bind failed");
        closesocket((SOCKET)m_udpSocket);
        m_udpSocket = 0;
        return;
    }

    char buffer[1024];
    sockaddr_in clientAddr;
    int clientAddrLen = sizeof(clientAddr);

    auto isCmd = [](const std::string& s, const char* prefix) {
        return s.rfind(prefix, 0) == 0;
    };

    while (!m_bStopUdpThread) {
        if (m_cameraCallbackId >= 0 && !m_cameraStream.running()) {
            try { m_pDevice->fisheyeCameras()->unregisterCallback(m_cameraCallbackId); } catch (...) {}
            m_cameraCallbackId = -1;
        }
        if (m_floorScanStatus.load() == 1 && SteadyNowSeconds() - m_floorScanStarted.load() > 4.0) {
            m_floorScanStatus.store(3);
            Log("Floor calibration timed out without fresh tracking poses.");
        }
        int bytesReceived = recvfrom((SOCKET)m_udpSocket, buffer, sizeof(buffer) - 1, 0, (sockaddr*)&clientAddr, &clientAddrLen);
        if (bytesReceived > 0) {
            buffer[bytesReceived] = '\0';
            std::string cmd(buffer);

            if (isCmd(cmd, "START_STREAMING")) {
                m_bStreamCalibrationData = true;
                inet_ntop(AF_INET, &clientAddr.sin_addr, m_clientIp, sizeof(m_clientIp));
                m_clientPort = ntohs(clientAddr.sin_port);
                Log("Started UDP streaming to %s:%d", m_clientIp, m_clientPort);
            }
            else if (isCmd(cmd, "STOP_STREAMING")) {
                m_bStreamCalibrationData = false;
                Log("Stopped UDP streaming");
            }
            else if (isCmd(cmd, "RELOAD_CONFIG")) {
                LoadConfig();
            }
            else if (isCmd(cmd, "SET_ROT_OFFSET")) {
                // Mount rotation X (body frame, HMD->device), quaternion w x y z
                float qw = 1, qx = 0, qy = 0, qz = 0;
                if (sscanf_s(cmd.c_str(), "SET_ROT_OFFSET %f %f %f %f", &qw, &qx, &qy, &qz) == 4) {
                    vr::EVRSettingsError err;
                    vr::VRSettings()->SetFloat("BSB-SLAM", "offset_qw", qw, &err);
                    vr::VRSettings()->SetFloat("BSB-SLAM", "offset_qx", qx, &err);
                    vr::VRSettings()->SetFloat("BSB-SLAM", "offset_qy", qy, &err);
                    vr::VRSettings()->SetFloat("BSB-SLAM", "offset_qz", qz, &err);
                    // Written in this driver's basis: without the version stamp LoadConfig
                    // treats the values as stale and silently restores the hardware default.
                    vr::VRSettings()->SetInt32("BSB-SLAM", "calibration_version", kCalibrationVersion, &err);
                    LoadConfig();
                    Log("SET_ROT_OFFSET: mount rotation set to (%.6f, %.6f, %.6f, %.6f).", qw, qx, qy, qz);
                }
            }
            else if (isCmd(cmd, "SET_WORLD_OFFSET")) {
                // World yaw C + translation t: qw qx qy qz tx ty tz
                float qw = 1, qx = 0, qy = 0, qz = 0, tx = 0, ty = 0, tz = 0;
                if (sscanf_s(cmd.c_str(), "SET_WORLD_OFFSET %f %f %f %f %f %f %f", &qw, &qx, &qy, &qz, &tx, &ty, &tz) == 7) {
                    vr::EVRSettingsError err;
                    vr::VRSettings()->SetFloat("BSB-SLAM", "world_qw", qw, &err);
                    vr::VRSettings()->SetFloat("BSB-SLAM", "world_qx", qx, &err);
                    vr::VRSettings()->SetFloat("BSB-SLAM", "world_qy", qy, &err);
                    vr::VRSettings()->SetFloat("BSB-SLAM", "world_qz", qz, &err);
                    vr::VRSettings()->SetFloat("BSB-SLAM", "world_x", tx, &err);
                    vr::VRSettings()->SetFloat("BSB-SLAM", "world_y", ty, &err);
                    vr::VRSettings()->SetFloat("BSB-SLAM", "world_z", tz, &err);
                    vr::VRSettings()->SetInt32("BSB-SLAM", "calibration_version", kCalibrationVersion, &err);
                    vr::VRSettings()->SetBool("BSB-SLAM", "world_valid", true, &err);
                    LoadConfig();
                }
            }
            else if (isCmd(cmd, "CLEAR_WORLD_OFFSET")) {
                vr::EVRSettingsError err;
                vr::VRSettings()->SetBool("BSB-SLAM", "world_valid", false, &err);
                LoadConfig();
            }
            else if (isCmd(cmd, "SET_OFFSET")) {
                // Lever arm L (device->head, device body frame)
                float lx = 0, ly = 0, lz = 0;
                if (sscanf_s(cmd.c_str(), "SET_OFFSET %f %f %f", &lx, &ly, &lz) == 3) {
                    vr::EVRSettingsError err;
                    vr::VRSettings()->SetFloat("BSB-SLAM", "offset_x", lx, &err);
                    vr::VRSettings()->SetFloat("BSB-SLAM", "offset_y", ly, &err);
                    vr::VRSettings()->SetFloat("BSB-SLAM", "offset_z", lz, &err);
                    vr::VRSettings()->SetInt32("BSB-SLAM", "calibration_version", kCalibrationVersion, &err);
                    LoadConfig();
                    Log("SET_OFFSET: mount lever arm set to (%.6f, %.6f, %.6f) m.", lx, ly, lz);
                }
            }
            else if (isCmd(cmd, "SET_PREDICTION")) {
                float seconds = 0.0f;
                if (sscanf_s(cmd.c_str(), "SET_PREDICTION %f", &seconds) == 1) {
                    seconds = std::max(0.0f, std::min(seconds, 0.05f));
                    vr::EVRSettingsError err;
                    vr::VRSettings()->SetFloat("BSB-SLAM", "prediction_s", seconds, &err);
                    LoadConfig();
                    Log("SET_PREDICTION: SDK prediction set to %.1f ms.", seconds * 1000.0f);
                }
            }
            else if (isCmd(cmd, "SET_TIME_OFFSET")) {
                float seconds = 0.0f;
                if (sscanf_s(cmd.c_str(), "SET_TIME_OFFSET %f", &seconds) == 1) {
                    seconds = std::max(-0.05f, std::min(seconds, 0.10f));
                    vr::EVRSettingsError err;
                    vr::VRSettings()->SetFloat("BSB-SLAM", "time_offset", seconds, &err);
                    LoadConfig();
                    Log("SET_TIME_OFFSET: extra SteamVR extrapolation set to %.1f ms.", seconds * 1000.0f);
                }
            }
            else if (isCmd(cmd, "SET_ANGULAR_VELOCITY")) {
                int enabled = 0;
                if (sscanf_s(cmd.c_str(), "SET_ANGULAR_VELOCITY %d", &enabled) == 1) {
                    vr::EVRSettingsError err;
                    vr::VRSettings()->SetBool("BSB-SLAM", "use_angular_velocity", enabled != 0, &err);
                    LoadConfig();
                    Log("SET_ANGULAR_VELOCITY: SteamVR angular prediction input %s.",
                        enabled != 0 ? "enabled" : "disabled");
                }
            }
            else if (isCmd(cmd, "SET_ANGULAR_LOCAL")) {
                int enabled = 0;
                if (sscanf_s(cmd.c_str(), "SET_ANGULAR_LOCAL %d", &enabled) == 1) {
                    vr::EVRSettingsError err;
                    vr::VRSettings()->SetBool("BSB-SLAM", "angular_velocity_local", enabled != 0, &err);
                    LoadConfig();
                    Log("SET_ANGULAR_LOCAL: angular velocity sent in %s space.",
                        enabled != 0 ? "headset-local" : "world");
                }
            }
            else if (isCmd(cmd, "SET_USE_CSLAM")) {
                int enabled = 0;
                if (sscanf_s(cmd.c_str(), "SET_USE_CSLAM %d", &enabled) == 1) {
                    vr::EVRSettingsError err;
                    vr::VRSettings()->SetBool("BSB-SLAM", "use_cslam", enabled != 0, &err);
                    LoadConfig();
                    Log("SET_USE_CSLAM: reference map loading %s on next SteamVR start.",
                        enabled != 0 ? "enabled" : "disabled");
                }
            }
            else if (cmd == "START_FLOOR_SCAN") {
                if (!m_bSlamReady.load() || !m_bSettled.load()) {
                    m_floorScanStatus.store(4);
                } else if (m_floorScanStatus.load() != 1) {
                    m_floorScanHeight.store(0.0);
                    m_floorScanStarted.store(SteadyNowSeconds());
                    m_floorScanStatus.store(1);
                    Log("Floor calibration started: keep headset still on floor for 2 seconds.");
                }
            }
            else if (cmd == "STOP_FLOOR_SCAN") {
                if (m_floorScanStatus.load() == 1) m_floorScanStatus.store(5);
            }
            else if (isCmd(cmd, "SET_FLOOR")) {
                m_floorScanStatus.store(5);
                // "The headset is currently on the floor" -> current head Y becomes floor.
                double curY;
                {
                    std::lock_guard<std::mutex> lock(m_poseMutex);
                    curY = m_pose.vecPosition[1];
                }
                m_floorAdjustY.store(m_floorAdjustY.load() - curY);
                Log("SET_FLOOR: floor set at current head height.");
            }
            else if (isCmd(cmd, "SET_HEIGHT")) {
                // "My head is currently H meters above the floor."
                float h = 0;
                if (sscanf_s(cmd.c_str(), "SET_HEIGHT %f", &h) == 1) {
                    if (!std::isfinite(h) || h < 0.0f || h > 2.5f) continue;
                    m_floorScanStatus.store(5);
                    double curY;
                    {
                        std::lock_guard<std::mutex> lock(m_poseMutex);
                        curY = m_pose.vecPosition[1];
                    }
                    m_floorAdjustY.store(m_floorAdjustY.load() + static_cast<double>(h) - curY);
                    vr::EVRSettingsError err;
                    vr::VRSettings()->SetFloat("BSB-SLAM", "head_height", h, &err);
                    LoadConfig();
                    Log("SET_HEIGHT: current head pose set to %.2f m above floor (persisted as default).", h);
                }
            }
            else if (isCmd(cmd, "SAVE_MAP")) {
                SaveCslamMap();
            }
            else if (isCmd(cmd, "RESET_SLAM")) {
                if (m_bSlamReady.load(std::memory_order_acquire)) {
                    Log("RESET_SLAM: resetting SLAM map and re-settling...");
                    m_bWorldTransformActive.store(false);
                    m_bSettled.store(false);
                    m_settleCounter.store(0);
                    m_bFloorCaptured.store(false);
                    m_floorAdjustY.store(0.0);
                    {
                        std::lock_guard<std::mutex> lock(m_levelMutex);
                        m_level = floor_level::Level();
                    }
                    m_levelReady.store(false); // the new map has its own vertical
                    m_levelDegrees.store(0.0);
                    m_floorScanStatus.store(0);
                    try { m_pSlam->reset(); } catch (...) { Log("RESET_SLAM: reset threw."); }
                }
            }
            else if (cmd == "START_CAMERA_STREAM") {
                const char* reply = "ERROR Camera unavailable";
                if (m_bSlamReady.load(std::memory_order_acquire) && m_pDevice && m_pDevice->fisheyeCameras()) {
                    if (m_cameraCallbackId >= 0) {
                        reply = "OK Camera stream already running";
                    } else if (m_cameraStream.start()) {
                        try {
                            m_cameraCallbackId = m_pDevice->fisheyeCameras()->registerCallback(
                                [this](const xv::FisheyeImages& pair) {
                                    if (pair.images.size() < 2) return;
                                    const auto& left = pair.images[0];
                                    const auto& right = pair.images[1];
                                    if (left.data && right.data && left.width == right.width && left.height == right.height)
                                        m_cameraStream.publish(left.data.get(), right.data.get(), left.width, left.height);
                                });
                        } catch (...) { m_cameraCallbackId = -1; }
                        if (m_cameraCallbackId >= 0) reply = "OK http://127.0.0.1:8422/";
                        else m_cameraStream.stop();
                    }
                }
                sendto((SOCKET)m_udpSocket, reply, (int)strlen(reply), 0, (sockaddr*)&clientAddr, clientAddrLen);
            }
            else if (cmd == "STOP_CAMERA_STREAM") {
                if (m_cameraCallbackId >= 0) {
                    try { m_pDevice->fisheyeCameras()->unregisterCallback(m_cameraCallbackId); } catch (...) {}
                    m_cameraCallbackId = -1;
                }
                m_cameraStream.stop();
                const char* reply = "OK Camera stream stopped";
                sendto((SOCKET)m_udpSocket, reply, (int)strlen(reply), 0, (sockaddr*)&clientAddr, clientAddrLen);
            }
            else if (cmd == "GET_CAMERA_CALIB") {
                std::string reply = "ERROR Camera calibration unavailable";
                if (m_bSlamReady.load(std::memory_order_acquire) && m_pDevice && m_pDevice->fisheyeCameras()) {
                    try {
                        const auto& cameras = m_pDevice->fisheyeCameras()->calibration();
                        if (!cameras.empty()) reply = CameraCalibrationJson(cameras);
                    } catch (...) {}
                }
                sendto((SOCKET)m_udpSocket, reply.c_str(), static_cast<int>(reply.size()), 0,
                       (sockaddr*)&clientAddr, clientAddrLen);
            }
            else if (isCmd(cmd, "GET_STATUS")) {
                char reply[1024];
                const double callbackTime = m_lastCallbackTime.load();
                const double callbackAgeMs = callbackTime > 0.0 ? (SteadyNowSeconds() - callbackTime) * 1000.0 : -1.0;
                snprintf(reply, sizeof(reply),
                    "STATUS slam_ready=%d settled=%d confidence=%.2f world_active=%d "
                    "cslam_load_attempted=%d streaming=%d camera_stream=%d sample_age_ms=%.2f pose_offset_ms=%.2f floor_scan=%d floor_height=%.3f get_pose_ms=%.2f pose_tick_ms=%.2f callback_age_ms=%.2f callback_submit_ms=%.2f sdk_prediction_ms=%.2f angular_frame=%d linear_frame=%d level_ready=%d level_deg=%.2f accel_norm=%.3f raw_pose_age_ms=%.2f",
                    (int)m_bSlamReady.load(), (int)m_bSettled.load(), m_lastConfidence.load(),
                    (int)m_bWorldTransformActive.load(), (int)m_bCslamLoadAttempted.load(),
                    (int)m_bStreamCalibrationData.load(), (int)m_cameraStream.running(), m_lastSampleAgeMs.load(),
                    m_lastPoseOffsetMs.load(), m_floorScanStatus.load(), m_floorScanHeight.load(),
                    m_lastGetPoseMs.load(), m_lastPoseTickMs.load(), callbackAgeMs, m_lastCallbackToSubmitMs.load(),
                    m_lastAppliedPredictionMs.load(), m_angularFrame.load(), m_linearFrame.load(),
                    (int)m_levelReady.load(), m_levelDegrees.load(),
                    m_accelNormReport.load(), m_lastRawPoseAgeMs.load());
                sendto((SOCKET)m_udpSocket, reply, (int)strlen(reply), 0, (sockaddr*)&clientAddr, clientAddrLen);
            }
            else if (isCmd(cmd, "GET_CONFIG")) {
                TrackerConfig cfg;
                {
                    std::lock_guard<std::mutex> lock(m_configMutex);
                    cfg = m_config;
                }
                char reply[1024];
                snprintf(reply, sizeof(reply),
                    "CONFIG version=%d offset_x=%.6f offset_y=%.6f offset_z=%.6f "
                    "offset_qw=%.6f offset_qx=%.6f offset_qy=%.6f offset_qz=%.6f "
                    "world_qw=%.6f world_qx=%.6f world_qy=%.6f world_qz=%.6f "
                    "world_x=%.6f world_y=%.6f world_z=%.6f world_valid=%d "
                    "prediction_s=%.6f time_offset=%.6f use_angular_velocity=%d "
                    "angular_velocity_local=%d head_height=%.6f use_cslam=%d",
                    cfg.calibration_version, cfg.lever_x, cfg.lever_y, cfg.lever_z,
                    cfg.mount_qw, cfg.mount_qx, cfg.mount_qy, cfg.mount_qz,
                    cfg.world_qw, cfg.world_qx, cfg.world_qy, cfg.world_qz,
                    cfg.world_x, cfg.world_y, cfg.world_z, (int)cfg.world_valid,
                    cfg.prediction_s, cfg.time_offset, (int)cfg.use_angular_velocity,
                    (int)cfg.angular_velocity_local, cfg.head_height, (int)cfg.use_cslam);
                sendto((SOCKET)m_udpSocket, reply, (int)strlen(reply), 0, (sockaddr*)&clientAddr, clientAddrLen);
            }
        } else {
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
    }

    if (m_cameraCallbackId >= 0) {
        try { m_pDevice->fisheyeCameras()->unregisterCallback(m_cameraCallbackId); } catch (...) {}
        m_cameraCallbackId = -1;
    }
    m_cameraStream.stop();
    Log("UDP IPC Thread stopped");
}

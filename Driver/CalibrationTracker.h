#pragma once

#include <openvr_driver.h>
#include <mutex>

// A second view of the same sensor, in the main tracker's room frame. It owns no
// SDK connection or worker threads and is never a headset override target.
class CCalibrationTracker : public vr::ITrackedDeviceServerDriver
{
public:
    static constexpr const char* kSerial = "SLAM-CAL";

    // Undo only the BODY-frame mount correction. Room/floor translations must
    // remain shared so one Space Calibrator transform aligns both devices.
    static vr::DriverPose_t SensorPose(const vr::DriverPose_t& head,
        const vr::HmdQuaternion_t& sensorRotation,
        const vr::HmdVector3d_t& worldLever, const vr::HmdVector3d_t& leverVelocity)
    {
        auto sensor = head;
        sensor.qRotation = sensorRotation;
        for (int axis = 0; axis < 3; ++axis) {
            sensor.vecPosition[axis] -= worldLever.v[axis];
            sensor.vecVelocity[axis] -= leverVelocity.v[axis];
        }
        return sensor;
    }

    CCalibrationTracker()
    {
        m_pose.qRotation.w = 1.0;
        m_pose.qWorldFromDriverRotation.w = 1.0;
        m_pose.qDriverFromHeadRotation.w = 1.0;
        m_pose.result = vr::TrackingResult_Uninitialized;
    }

    vr::EVRInitError Activate(uint32_t id) override
    {
        std::lock_guard<std::mutex> lock(m_mutex);
        m_id = id;
        auto container = vr::VRProperties()->TrackedDeviceToPropertyContainer(id);
        vr::VRProperties()->SetStringProperty(container, vr::Prop_TrackingSystemName_String, "xvisio_slam");
        vr::VRProperties()->SetStringProperty(container, vr::Prop_ModelNumber_String, kSerial);
        vr::VRProperties()->SetStringProperty(container, vr::Prop_SerialNumber_String, kSerial);
        vr::VRProperties()->SetInt32Property(container, vr::Prop_DeviceClass_Int32, vr::TrackedDeviceClass_GenericTracker);
        vr::VRProperties()->SetInt32Property(container, vr::Prop_ControllerRoleHint_Int32, vr::TrackedControllerRole_Invalid);
        vr::VRProperties()->SetStringProperty(container, vr::Prop_RenderModelName_String, "{htc}/rendermodels/vr_tracker_vive_1_0");
        return vr::VRInitError_None;
    }

    void Deactivate() override
    {
        std::lock_guard<std::mutex> lock(m_mutex);
        m_id = vr::k_unTrackedDeviceIndexInvalid;
    }
    void EnterStandby() override {}
    void* GetComponent(const char*) override { return nullptr; }
    void DebugRequest(const char*, char* response, uint32_t size) override
    {
        if (size) response[0] = '\0';
    }
    vr::DriverPose_t GetPose() override
    {
        std::lock_guard<std::mutex> lock(m_mutex);
        return m_pose;
    }
    void Publish(const vr::DriverPose_t& pose)
    {
        std::lock_guard<std::mutex> lock(m_mutex);
        m_pose = pose;
        if (m_id != vr::k_unTrackedDeviceIndexInvalid)
            vr::VRServerDriverHost()->TrackedDevicePoseUpdated(m_id, m_pose, sizeof(m_pose));
    }
    void PublishStatus(const vr::DriverPose_t& status)
    {
        std::lock_guard<std::mutex> lock(m_mutex);
        m_pose.poseIsValid = false;
        m_pose.deviceIsConnected = status.deviceIsConnected;
        m_pose.result = status.result;
        if (m_id != vr::k_unTrackedDeviceIndexInvalid)
            vr::VRServerDriverHost()->TrackedDevicePoseUpdated(m_id, m_pose, sizeof(m_pose));
    }

private:
    std::mutex m_mutex;
    vr::TrackedDeviceIndex_t m_id{vr::k_unTrackedDeviceIndexInvalid};
    vr::DriverPose_t m_pose{};
};

#include "../Driver/CalibrationTracker.h"
#include <cassert>
#include <cstring>

int main()
{
    CCalibrationTracker tracker;
    assert(std::strcmp(CCalibrationTracker::kSerial, "SLAM-CAL") == 0);
    assert(!tracker.GetPose().poseIsValid);
    vr::DriverPose_t raw{};
    raw.qWorldFromDriverRotation.w = raw.qDriverFromHeadRotation.w = 1.0;
    raw.qRotation = {0.5, 0.5, -0.5, -0.5};
    raw.vecPosition[0] = 1.2;
    raw.vecPosition[1] = -0.3;
    raw.vecPosition[2] = -2.4;
    raw.vecVelocity[0] = 0.2;
    raw.poseTimeOffset = 0.003;
    raw.deviceIsConnected = raw.poseIsValid = true;
    raw.result = vr::TrackingResult_Running_OK;
    tracker.Publish(raw);
    auto pose = tracker.GetPose();
    assert(pose.poseIsValid && pose.vecPosition[1] == -0.3);
    assert(pose.qRotation.y == -0.5 && pose.vecVelocity[0] == 0.2);
    assert(pose.poseTimeOffset == 0.003);
    // A loss update must retain the sensor pose, never copy the head's offsets.
    auto head = raw;
    head.vecPosition[1] = 1.8;
    head.result = vr::TrackingResult_Running_OutOfRange;
    tracker.PublishStatus(head);
    pose = tracker.GetPose();
    assert(!pose.poseIsValid && pose.deviceIsConnected);
    assert(pose.result == vr::TrackingResult_Running_OutOfRange);
    assert(pose.vecPosition[1] == -0.3);
    tracker.Publish(raw);
    assert(tracker.GetPose().poseIsValid);

    // A large room/floor translation belongs to both poses. Only the physical
    // lever arm and its rotational velocity should separate head and sensor.
    head = raw;
    head.vecPosition[1] = 2.128 - 1.194;
    head.vecVelocity[0] = 0.5;
    const vr::HmdVector3d_t arm{{0.01, 0.005, 0.051}};
    const vr::HmdVector3d_t armVelocity{{0.3, 0.0, 0.0}};
    auto sensor = CCalibrationTracker::SensorPose(head, raw.qRotation, arm, armVelocity);
    assert(sensor.vecPosition[1] == head.vecPosition[1] - 0.005);
    assert(sensor.vecPosition[2] == head.vecPosition[2] - 0.051);
    assert(sensor.vecVelocity[0] == head.vecVelocity[0] - 0.3);
    assert(sensor.qRotation.y == raw.qRotation.y);
    assert(sensor.poseTimeOffset == head.poseTimeOffset);
    assert(sensor.poseIsValid && sensor.deviceIsConnected);
}

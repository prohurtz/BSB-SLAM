#pragma once

#include <openvr_driver.h>
#include "GenericTracker.h"
#include <vector>

class CServerDriver : public vr::IServerTrackedDeviceProvider
{
public:
    CServerDriver();
    virtual ~CServerDriver();
    virtual vr::EVRInitError Init(vr::IVRDriverContext* pDriverContext) override;
    virtual void Cleanup() override;
    virtual const char* const* GetInterfaceVersions() override;
    virtual void RunFrame() override;
    virtual bool ShouldBlockStandbyMode() override;
    virtual void EnterStandby() override;
    virtual void LeaveStandby() override;

private:
    CCalibrationTracker m_calibrationTracker;
    CGenericTracker* m_pSlamTracker;
};

#include "ServerDriver.h"

CServerDriver::CServerDriver() : m_pSlamTracker(NULL) {}
CServerDriver::~CServerDriver() {}

vr::EVRInitError CServerDriver::Init(vr::IVRDriverContext* pDriverContext)
{
    VR_INIT_SERVER_DRIVER_CONTEXT(pDriverContext);

    // Create our virtual tracker device
    if (!vr::VRServerDriverHost()->TrackedDeviceAdded(CCalibrationTracker::kSerial,
        vr::TrackedDeviceClass_GenericTracker, &m_calibrationTracker))
        return vr::VRInitError_Driver_Failed;
    m_pSlamTracker = new CGenericTracker(&m_calibrationTracker);
    vr::VRServerDriverHost()->TrackedDeviceAdded(m_pSlamTracker->GetSerialNumber().c_str(), vr::TrackedDeviceClass_GenericTracker, m_pSlamTracker);

    return vr::VRInitError_None;
}

void CServerDriver::Cleanup()
{
    if (m_pSlamTracker)
    {
        delete m_pSlamTracker;
        m_pSlamTracker = NULL;
    }
}

const char* const* CServerDriver::GetInterfaceVersions()
{
    return vr::k_InterfaceVersions;
}

void CServerDriver::RunFrame()
{
    if (m_pSlamTracker)
    {
        m_pSlamTracker->RunFrame();
    }
}

bool CServerDriver::ShouldBlockStandbyMode()
{
    return false; // This driver does not need to block
}

void CServerDriver::EnterStandby()
{
    if (m_pSlamTracker)
    {
        m_pSlamTracker->EnterStandby();
    }
}

void CServerDriver::LeaveStandby()
{
}

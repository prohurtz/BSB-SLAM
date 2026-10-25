/*
* =================================================================================
* file: driver_main.cpp
* =================================================================================
*/
#include "ServerDriver.h" 

#ifndef HMD_DLL_EXPORT
#if defined(_WIN32)
#define HMD_DLL_EXPORT extern "C" __declspec( dllexport )
#elif defined(__GNUC__) || defined(COMPILER_GCC) || defined(__APPLE__)
#define HMD_DLL_EXPORT extern "C"
#else
#error "Unsupported Platform"
#endif
#endif

// Global instance of our driver provider
CServerDriver g_serverDriver;

// The factory function that provides the driver to SteamVR
HMD_DLL_EXPORT
void* HmdDriverFactory(const char* pInterfaceName, int* pReturnCode)
{
    if (0 == strcmp(vr::IServerTrackedDeviceProvider_Version, pInterfaceName))
    {
        return &g_serverDriver;
    }

    if (pReturnCode)
        *pReturnCode = vr::VRInitError_Init_InterfaceNotFound;

    return NULL;
}

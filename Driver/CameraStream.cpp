#define NOMINMAX
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <wincodec.h>
#include <objbase.h>
#include <algorithm>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <string>
#include <utility>
#include "CameraStream.h"
#include "StereoFrame.h"
#pragma comment(lib, "Ws2_32.lib")
#pragma comment(lib, "Windowscodecs.lib")
#pragma comment(lib, "Ole32.lib")

namespace {
template<class T> void release(T*& p) { if (p) { p->Release(); p = nullptr; } }

bool send_all(SOCKET socket, const char* data, size_t size) {
    while (size) {
        const int n = send(socket, data, static_cast<int>(std::min<size_t>(size, 32768)), 0);
        if (n <= 0) return false;
        data += n; size -= n;
    }
    return true;
}

std::vector<uint8_t> jpeg_gray(const std::vector<uint8_t>& pixels, int width, int height,
                               IWICImagingFactory* factory) {
    std::vector<uint8_t> result;
    IWICBitmapEncoder* encoder = nullptr;
    IWICBitmapFrameEncode* frame = nullptr;
    IPropertyBag2* properties = nullptr;
    HGLOBAL memory = GlobalAlloc(GMEM_MOVEABLE, 1);
    if (!memory) return result;
    IStream* output = nullptr;
    if (FAILED(CreateStreamOnHGlobal(memory, TRUE, &output))) { GlobalFree(memory); return result; }
    if (SUCCEEDED(factory->CreateEncoder(GUID_ContainerFormatJpeg, nullptr, &encoder)) &&
        SUCCEEDED(encoder->Initialize(output, WICBitmapEncoderNoCache)) &&
        SUCCEEDED(encoder->CreateNewFrame(&frame, &properties)) &&
        SUCCEEDED(frame->Initialize(properties)) &&
        SUCCEEDED(frame->SetSize(width, height))) {
        WICPixelFormatGUID format = GUID_WICPixelFormat8bppGray;
        if (SUCCEEDED(frame->SetPixelFormat(&format)) && IsEqualGUID(format, GUID_WICPixelFormat8bppGray) &&
            SUCCEEDED(frame->WritePixels(static_cast<UINT>(height), static_cast<UINT>(width),
                                         static_cast<UINT>(pixels.size()), const_cast<BYTE*>(pixels.data()))) &&
            SUCCEEDED(frame->Commit()) && SUCCEEDED(encoder->Commit())) {
            HGLOBAL encoded = nullptr;
            STATSTG stat = {};
            if (SUCCEEDED(output->Stat(&stat, STATFLAG_NONAME)) &&
                SUCCEEDED(GetHGlobalFromStream(output, &encoded))) {
                const size_t bytes = static_cast<size_t>(stat.cbSize.QuadPart);
                if (const void* data = GlobalLock(encoded)) {
                    result.assign(static_cast<const uint8_t*>(data), static_cast<const uint8_t*>(data) + bytes);
                    GlobalUnlock(encoded);
                }
            }
        }
    }
    release(properties); release(frame); release(encoder); release(output);
    return result;
}
}

CameraStream::~CameraStream() { stop(); }

bool CameraStream::start() {
    if (worker_.joinable() && !running_) worker_.join();
    if (running_.exchange(true)) return true;
    listening_ = false;
    bound_port_ = 0;
    try { worker_ = std::thread(&CameraStream::serve, this); }
    catch (...) { running_ = false; return false; }
    for (int i = 0; i < 100 && running_ && !listening_; ++i)
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    if (listening_) return true;
    stop();
    return false;
}

void CameraStream::stop() {
    running_ = false;
    listening_ = false;
    bound_port_ = 0;
    if (worker_.joinable()) worker_.join();
    std::lock_guard<std::mutex> lock(mutex_);
    gray_.clear(); width_ = height_ = 0;
}

void CameraStream::publish(const uint8_t* left, const uint8_t* right, int width, int height) {
    if (!running_) return;
    auto joined = stereo_frame::side_by_side(left, right, width, height);
    if (joined.empty() || !running_) return;
    std::lock_guard<std::mutex> lock(mutex_);
    if (!running_) return;
    width_ = width * 2; height_ = height;
    gray_ = std::move(joined);
    ++sequence_;
}

void CameraStream::serve() {
    const HRESULT com = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    IWICImagingFactory* factory = nullptr;
    if (FAILED(com) || FAILED(CoCreateInstance(CLSID_WICImagingFactory, nullptr, CLSCTX_INPROC_SERVER,
            IID_PPV_ARGS(&factory)))) { running_ = false; if (SUCCEEDED(com)) CoUninitialize(); return; }
    SOCKET listener = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    sockaddr_in address = {};
    address.sin_family = AF_INET;
    address.sin_port = htons(static_cast<u_short>(port_));
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (listener == INVALID_SOCKET || bind(listener, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == SOCKET_ERROR ||
        listen(listener, 2) == SOCKET_ERROR) {
        if (listener != INVALID_SOCKET) closesocket(listener);
        release(factory); CoUninitialize(); running_ = false; return;
    }
    sockaddr_in bound = {};
    int boundSize = sizeof(bound);
    if (getsockname(listener, reinterpret_cast<sockaddr*>(&bound), &boundSize) == 0)
        bound_port_ = ntohs(bound.sin_port);
    listening_ = true;
    std::vector<SOCKET> viewers;
    uint64_t seen = 0;
    while (running_) {
        fd_set reads; FD_ZERO(&reads); FD_SET(listener, &reads);
        timeval timeout = {0, 50000};
        if (select(0, &reads, nullptr, nullptr, &timeout) > 0) {
            SOCKET client = accept(listener, nullptr, nullptr);
            if (client != INVALID_SOCKET) {
                DWORD sendTimeout = 1000, receiveTimeout = 1000;
                setsockopt(client, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char*>(&sendTimeout), sizeof(sendTimeout));
                setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&receiveTimeout), sizeof(receiveTimeout));
                char request[1024] = {};
                const int n = recv(client, request, sizeof(request) - 1, 0);
                if (n > 0 && strncmp(request, "GET /stereo.mjpg ", 17) == 0) {
                    const char header[] = "HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=frame\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n";
                    if (send_all(client, header, strlen(header)) && viewers.size() < 8) {
                        viewers.push_back(client);
                        seen = 0; // send the current frame immediately to the new viewer
                        continue;
                    }
                } else if (n > 0 && strncmp(request, "GET /snapshot.jpg", 17) == 0 &&
                           (request[17] == ' ' || request[17] == '?')) {
                    std::vector<uint8_t> pixels;
                    int width = 0, height = 0;
                    {
                        std::lock_guard<std::mutex> lock(mutex_);
                        pixels = gray_;
                        width = width_;
                        height = height_;
                    }
                    const auto jpg = pixels.empty() ? std::vector<uint8_t>{} : jpeg_gray(pixels, width, height, factory);
                    if (jpg.empty()) {
                        const char unavailable[] = "HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n";
                        send_all(client, unavailable, strlen(unavailable));
                    } else {
                        char header[256];
                        snprintf(header, sizeof(header), "HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: %zu\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n", jpg.size());
                        send_all(client, header, strlen(header));
                        send_all(client, reinterpret_cast<const char*>(jpg.data()), jpg.size());
                    }
                } else if (n > 0 && strncmp(request, "GET / ", 6) == 0) {
                    const char body[] = "<!doctype html><title>XR50 stereo</title><style>body{background:#111;color:white;font:16px sans-serif}img{max-width:100%}</style><h1>XR50 stereo</h1><p>Left | Right</p><img src='/stereo.mjpg'>";
                    char header[256];
                    snprintf(header, sizeof(header), "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: %zu\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n", strlen(body));
                    send_all(client, header, strlen(header)); send_all(client, body, strlen(body));
                } else {
                    const char bad[] = "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
                    send_all(client, bad, strlen(bad));
                }
                closesocket(client);
            }
        }
        if (viewers.empty()) continue;
        std::vector<uint8_t> pixels; int width = 0, height = 0;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (sequence_ == seen || gray_.empty()) continue;
            seen = sequence_; pixels = gray_; width = width_; height = height_;
        }
        const auto jpg = jpeg_gray(pixels, width, height, factory);
        if (jpg.empty()) continue;
        char part[160];
        snprintf(part, sizeof(part), "--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %zu\r\n\r\n", jpg.size());
        for (size_t i = 0; i < viewers.size();) {
            SOCKET viewer = viewers[i];
            if (!send_all(viewer, part, strlen(part)) ||
                !send_all(viewer, reinterpret_cast<const char*>(jpg.data()), jpg.size()) ||
                !send_all(viewer, "\r\n", 2)) {
                closesocket(viewer);
                viewers.erase(viewers.begin() + i);
            } else ++i;
        }
    }
    for (SOCKET viewer : viewers) closesocket(viewer);
    listening_ = false;
    bound_port_ = 0;
    closesocket(listener); release(factory); CoUninitialize();
}

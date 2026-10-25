#pragma once

#include <atomic>
#include <cstdint>
#include <mutex>
#include <thread>
#include <vector>

// Optional localhost preview. The SDK callback only copies the newest stereo
// frame; JPEG encoding and network I/O run on this separate worker.
class CameraStream {
public:
    static constexpr int kPort = 8422;
    explicit CameraStream(int port = kPort) : port_(port) {}
    ~CameraStream();
    bool start();
    void stop();
    bool running() const { return running_.load(); }
    int port() const { return bound_port_.load(); }
    void publish(const uint8_t* left, const uint8_t* right, int width, int height);

private:
    void serve();
    std::atomic<bool> running_{false};
    std::atomic<bool> listening_{false};
    const int port_;
    std::atomic<int> bound_port_{0};
    std::thread worker_;
    std::mutex mutex_;
    std::vector<uint8_t> gray_;
    int width_{0}, height_{0};
    uint64_t sequence_{0};
};

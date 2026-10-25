#pragma once

#include <algorithm>
#include <cmath>

// SDK headers do not state whether angularVelocity() is in SLAM world or
// device-body coordinates. Infer the frame from pose changes before passing
// it to SteamVR's prediction engine.
namespace angular_velocity {
struct Vec3 { double x, y, z; };
struct Quat { double w, x, y, z; };
enum class Frame { Unknown = 0, World = 1, Body = 2 };

inline Quat conjugate(Quat q) { return {q.w, -q.x, -q.y, -q.z}; }
inline Quat multiply(Quat a, Quat b) {
    return {a.w*b.w - a.x*b.x - a.y*b.y - a.z*b.z,
            a.w*b.x + a.x*b.w + a.y*b.z - a.z*b.y,
            a.w*b.y - a.x*b.z + a.y*b.w + a.z*b.x,
            a.w*b.z + a.x*b.y - a.y*b.x + a.z*b.w};
}
inline Vec3 rotate(Quat q, Vec3 v) {
    const Quat r = multiply(multiply(q, {0, v.x, v.y, v.z}), conjugate(q));
    return {r.x, r.y, r.z};
}
inline double norm2(Vec3 v) { return v.x*v.x + v.y*v.y + v.z*v.z; }
inline double error2(Vec3 a, Vec3 b) { return norm2({a.x-b.x, a.y-b.y, a.z-b.z}); }

// All three inputs must share the world frame. In particular, use the selector's
// validated world angular velocity, never an unconverted SDK body-frame value.
inline Vec3 point_velocity(Vec3 linear, Vec3 world_omega, Vec3 world_arm) {
    return {linear.x + world_omega.y*world_arm.z - world_omega.z*world_arm.y,
            linear.y + world_omega.z*world_arm.x - world_omega.x*world_arm.z,
            linear.z + world_omega.x*world_arm.y - world_omega.y*world_arm.x};
}

inline Vec3 midpoint(Vec3 a, Vec3 b) { return {(a.x + b.x)*.5, (a.y + b.y)*.5, (a.z + b.z)*.5}; }

// Running vote between the SDK value read as world-frame and as body-frame, scored
// against a rate observed from pose changes. max_error2 bounds the accepted squared
// error; same2 is the squared difference below which both readings coincide. Once a
// frame is established it is kept through noisy stretches (the SDK's frame does not
// change at runtime) and only replaced when the other frame clearly fits better;
// dropping back to Unknown would zero SteamVR's prediction mid-session.
class FrameVote {
public:
    FrameVote(double max_error2, double same2) : max_error2_(max_error2), same2_(same2) {}
    void add(Vec3 observed, Vec3 world_mid, Vec3 body_mid) {
        const double ew = error2(observed, world_mid);
        const double eb = error2(observed, body_mid);
        world_error_ = samples_ ? .9*world_error_ + .1*ew : ew;
        body_error_ = samples_ ? .9*body_error_ + .1*eb : eb;
        const double difference = error2(world_mid, body_mid);
        frame_difference_ = samples_ ? .9*frame_difference_ + .1*difference : difference;
        ++samples_;
        if (samples_ >= 12) {
            const Frame decided = world_error_ < max_error2_ && frame_difference_ < same2_ ? Frame::World
                                : world_error_ < max_error2_ && world_error_ < body_error_*.65 ? Frame::World
                                : body_error_ < max_error2_ && body_error_ < world_error_*.65 ? Frame::Body
                                : Frame::Unknown;
            if (decided != Frame::Unknown) frame_ = decided;
        }
    }
    Frame frame() const { return frame_; }
private:
    double max_error2_, same2_;
    Frame frame_ = Frame::Unknown;
    double world_error_ = 0, body_error_ = 0;
    double frame_difference_ = 0;
    int samples_ = 0;
};

inline bool finite(double time, Quat q, Vec3 v) {
    return std::isfinite(time) && std::isfinite(q.w) && std::isfinite(q.x) && std::isfinite(q.y) &&
           std::isfinite(q.z) && std::isfinite(v.x) && std::isfinite(v.y) && std::isfinite(v.z);
}

class Selector {
public:
    Frame update(double time, Quat q, Vec3 sdk) {
        if (!finite(time, q, sdk) || norm2(sdk) > 400) return vote_.frame();
        const Vec3 body = rotate(q, sdk);
        if (previous_time_ > 0) {
            const double dt = time - previous_time_;
            if (dt >= .012 && dt <= .05) {
                Quat delta = multiply(q, conjugate(previous_q_));
                if (delta.w < 0) delta = {-delta.w, -delta.x, -delta.y, -delta.z};
                const double s = std::sqrt(delta.x*delta.x + delta.y*delta.y + delta.z*delta.z);
                if (s > 1e-6) {
                    const double rate = 2 * std::atan2(s, delta.w) / (s * dt);
                    Vec3 observed{delta.x*rate, delta.y*rate, delta.z*rate};
                    if (norm2(observed) >= .09 && norm2(observed) <= 100)
                        vote_.add(observed, midpoint(sdk, previous_sdk_), midpoint(body, previous_body_));
                }
            }
        }
        if (previous_time_ <= 0 || time - previous_time_ >= .012) {
            previous_time_ = time; previous_q_ = q; previous_sdk_ = sdk; previous_body_ = body;
        }
        return vote_.frame();
    }
    Vec3 world_velocity(Quat q, Vec3 sdk) const {
        const Frame f = vote_.frame();
        return f == Frame::World ? sdk : f == Frame::Body ? rotate(q, sdk) : Vec3{0, 0, 0};
    }
    Frame frame() const { return vote_.frame(); }
    void reset() { *this = Selector(); }
private:
    FrameVote vote_{.36, .01};
    double previous_time_ = 0;
    Quat previous_q_{1, 0, 0, 0};
    Vec3 previous_sdk_{0, 0, 0}, previous_body_{0, 0, 0};
};

// linearVelocity() has the same undocumented frame. On an upside-down mount a body-frame
// value read as world-frame inverts vertical and lateral motion, so SteamVR would
// extrapolate the head the wrong way. Validate it against position changes too.
class LinearSelector {
public:
    Frame update(double time, Quat q, Vec3 position, Vec3 sdk) {
        if (!finite(time, q, sdk) || !std::isfinite(position.x) || !std::isfinite(position.y) ||
            !std::isfinite(position.z) || norm2(sdk) > 100) return vote_.frame();
        const Vec3 body = rotate(q, sdk);
        if (previous_time_ > 0) {
            const double dt = time - previous_time_;
            if (dt >= .012 && dt <= .05) {
                const Vec3 observed{(position.x - previous_position_.x)/dt,
                                    (position.y - previous_position_.y)/dt,
                                    (position.z - previous_position_.z)/dt};
                if (norm2(observed) >= .01 && norm2(observed) <= 25)
                    vote_.add(observed, midpoint(sdk, previous_sdk_), midpoint(body, previous_body_));
            }
        }
        if (previous_time_ <= 0 || time - previous_time_ >= .012) {
            previous_time_ = time; previous_position_ = position; previous_sdk_ = sdk; previous_body_ = body;
        }
        return vote_.frame();
    }
    Vec3 world_velocity(Quat q, Vec3 sdk) const {
        const Frame f = vote_.frame();
        return f == Frame::World ? sdk : f == Frame::Body ? rotate(q, sdk) : Vec3{0, 0, 0};
    }
    Frame frame() const { return vote_.frame(); }
    void reset() { *this = LinearSelector(); }
private:
    FrameVote vote_{.09, .0025};
    double previous_time_ = 0;
    Vec3 previous_position_{0, 0, 0};
    Vec3 previous_sdk_{0, 0, 0}, previous_body_{0, 0, 0};
};
}

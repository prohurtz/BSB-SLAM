#include "../Driver/AngularVelocity.h"
#include <cmath>
#include <stdexcept>

void require(bool ok) { if (!ok) throw std::runtime_error("angular velocity selector check failed"); }

int main() {
    using namespace angular_velocity;
    const Quat tilt{std::cos(.45), std::sin(.45), 0, 0};
    const Vec3 omega{0, 0, 1.2};
    for (Frame expected : {Frame::World, Frame::Body}) {
        Selector selector;
        for (int i = 0; i < 50; ++i) {
            const double t = 1.0 + i*.016;
            const double a = 1.2 * (t - 1.0) * .5;
            const Quat q = multiply({std::cos(a), 0, 0, std::sin(a)}, tilt);
            const Vec3 sdk = expected == Frame::World ? omega : rotate(conjugate(q), omega);
            selector.update(t, q, sdk);
        }
        require(selector.frame() == expected);
        const Vec3 converted = selector.world_velocity(tilt, expected == Frame::World ? omega : rotate(conjugate(tilt), omega));
        require(error2(converted, omega) < 1e-6);
        const Vec3 arm = rotate(tilt, {0, 0, .052});
        const Vec3 transferred = point_velocity({.1, .2, .3}, converted, arm);
        require(error2(transferred, {.1 - 1.2*arm.y, .2 + 1.2*arm.x, .3}) < 1e-6);
        // This non-axis-aligned pose makes a mistaken body/world mix detectable.
        if (expected == Frame::Body)
            require(error2(transferred, point_velocity({.1, .2, .3}, rotate(conjugate(tilt), omega), arm)) > 1e-5);
    }
    Selector unknown;
    require(unknown.frame() == Frame::Unknown);
    require(norm2(unknown.world_velocity(tilt, omega)) == 0);
    Selector yaw_only;
    for (int i = 0; i < 50; ++i) {
        const double t = 1.0 + i*.016;
        const double a = 1.2 * (t - 1.0) * .5;
        yaw_only.update(t, {std::cos(a), 0, 0, std::sin(a)}, omega);
    }
    require(yaw_only.frame() == Frame::World); // both possible SDK frames coincide

    // Linear velocity on an upside-down, slightly pitched mount: a body-frame value
    // read as world-frame would invert vertical motion, so it must be detected.
    const Quat upside_down = multiply({0, 0, 0, 1}, {std::cos(.087), std::sin(.087), 0, 0});
    const Vec3 nod{0, -.4, .2};
    for (Frame expected : {Frame::World, Frame::Body}) {
        LinearSelector linear;
        for (int i = 0; i < 50; ++i) {
            const double t = 1.0 + i*.016;
            const Vec3 position{nod.x*(t - 1), nod.y*(t - 1), nod.z*(t - 1)};
            const Vec3 sdk = expected == Frame::World ? nod : rotate(conjugate(upside_down), nod);
            linear.update(t, upside_down, position, sdk);
        }
        require(linear.frame() == expected);
        const Vec3 sdk = expected == Frame::World ? nod : rotate(conjugate(upside_down), nod);
        require(error2(linear.world_velocity(upside_down, sdk), nod) < 1e-9);
    }
    LinearSelector still;
    for (int i = 0; i < 50; ++i) still.update(1.0 + i*.016, upside_down, {0, 0, 0}, {.01, 0, 0});
    require(still.frame() == Frame::Unknown); // no movement, no evidence
    require(norm2(still.world_velocity(upside_down, nod)) == 0);
    LinearSelector wrong;
    for (int i = 0; i < 50; ++i) {
        const double t = 1.0 + i*.016;
        wrong.update(t, upside_down, {nod.x*(t - 1), nod.y*(t - 1), nod.z*(t - 1)}, {.5, .5, -.5});
    }
    require(wrong.frame() == Frame::Unknown); // neither reading matches the motion

    // An established frame survives a noisy stretch instead of zeroing prediction.
    LinearSelector sticky;
    double t = 1.0;
    Vec3 position{0, 0, 0};
    for (int i = 0; i < 50; ++i, t += .016) {
        sticky.update(t, upside_down, position, nod);
        position = {position.x + nod.x*.016, position.y + nod.y*.016, position.z + nod.z*.016};
    }
    require(sticky.frame() == Frame::World);
    for (int i = 0; i < 80; ++i, t += .016) {
        const Vec3 noisy{nod.x + (i % 2 ? .8 : -.8), nod.y, nod.z + (i % 3 ? .6 : -.6)};
        sticky.update(t, upside_down, position, noisy);
        position = {position.x + nod.x*.016, position.y + nod.y*.016, position.z + nod.z*.016};
    }
    require(sticky.frame() == Frame::World);
    // ...but still switches when the other frame clearly fits.
    for (int i = 0; i < 80; ++i, t += .016) {
        sticky.update(t, upside_down, position, rotate(conjugate(upside_down), nod));
        position = {position.x + nod.x*.016, position.y + nod.y*.016, position.z + nod.z*.016};
    }
    require(sticky.frame() == Frame::Body);
}

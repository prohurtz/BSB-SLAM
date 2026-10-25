#pragma once

#include <algorithm>
#include <cmath>

#include "AngularVelocity.h"

// Session-only floor leveling. While the headset rests still on the floor, the mean
// accelerometer reading points "up" (specific force opposes gravity). If the SLAM world
// is tilted, that direction is not OpenVR +Y; rotating the session frame by the shortest
// arc between them levels the floor everywhere, not just at one point.
namespace floor_level {
using angular_velocity::Quat;
using angular_velocity::Vec3;

// Rigid correction applied to world poses: p' = q * p + t.
struct Level {
    Quat q{1, 0, 0, 0};
    Vec3 t{0, 0, 0};
    Vec3 apply(Vec3 p) const {
        const Vec3 r = angular_velocity::rotate(q, p);
        return {r.x + t.x, r.y + t.y, r.z + t.z};
    }
};

// Shortest rotation taking `up` onto +Y. Fails for degenerate input or a tilt above
// max_degrees, which signals a sensor/axis mismatch rather than a real SLAM tilt.
inline bool rotation_to_up(Vec3 up, double max_degrees, Quat& out, double& degrees) {
    const double n = std::sqrt(angular_velocity::norm2(up));
    if (!std::isfinite(n) || n < 1e-9) return false;
    const Vec3 u{up.x/n, up.y/n, up.z/n};
    const double c = std::max(-1.0, std::min(1.0, u.y));
    degrees = std::acos(c) * 180.0 / 3.14159265358979323846;
    if (degrees > max_degrees) return false;
    // q = normalize(1 + u.Y, u x Y); u x Y = (-u.z, 0, u.x).
    Quat q{1 + c, -u.z, 0, u.x};
    const double qn = std::sqrt(q.w*q.w + q.x*q.x + q.y*q.y + q.z*q.z);
    out = {q.w/qn, q.x/qn, q.y/qn, q.z/qn};
    return true;
}

// Follow `level` with rotation r about `pivot` (a point in the already-leveled frame),
// so the pivot itself does not move.
inline Level compose(const Level& level, Quat r, Vec3 pivot) {
    const Vec3 rt = angular_velocity::rotate(r, {level.t.x - pivot.x, level.t.y - pivot.y, level.t.z - pivot.z});
    return {angular_velocity::multiply(r, level.q), {rt.x + pivot.x, rt.y + pivot.y, rt.z + pivot.z}};
}
}

#pragma once

#include <algorithm>
#include <cmath>
#include <vector>

// Independent geometry-only floor selector for Xvisio's detected planes.
// The room-layout prototype motivated early floor calibration, but its
// point-cloud histogram, semantic labels, and plane-fitting code are not used
// here. Inputs are gravity-aligned OpenVR-world metres.
namespace floor_detection {
struct Point { double x, y, z; };
struct Plane { double normal_y; std::vector<Point> border; };
struct Result { bool valid = false; double floor_y = 0; double head_clearance_m = 0; int support = 0; };

inline Result estimate_planes(const std::vector<Plane>& planes, double head_y) {
    if (!std::isfinite(head_y)) return {};
    Result best;
    for (const auto& plane : planes) {
        if (std::abs(plane.normal_y) < 0.985 || plane.border.size() < 3) continue;
        double sum = 0, xmin = 1e9, xmax = -1e9, zmin = 1e9, zmax = -1e9;
        for (const auto& p : plane.border) {
            if (!std::isfinite(p.x) || !std::isfinite(p.y) || !std::isfinite(p.z)) { sum = NAN; break; }
            sum += p.y;
            xmin = std::min(xmin, p.x); xmax = std::max(xmax, p.x);
            zmin = std::min(zmin, p.z); zmax = std::max(zmax, p.z);
        }
        const double floor_y = sum / plane.border.size();
        const double clearance = head_y - floor_y;
        if (!std::isfinite(floor_y) || clearance < 1.0 || clearance > 2.5 ||
            xmax - xmin < 0.45 || zmax - zmin < 0.45) continue;
        if (!best.valid || floor_y < best.floor_y)
            best = {true, floor_y, clearance, static_cast<int>(plane.border.size())};
    }
    return best;
}

class StableFloor {
public:
    bool add(const Result& result) {
        if (!result.valid) { consecutive_ = 0; return false; }
        consecutive_ = std::abs(result.floor_y - last_) <= 0.03 ? consecutive_ + 1 : 1;
        last_ = result.floor_y;
        if (consecutive_ >= 3) { measured_ = result; return true; }
        return false;
    }
    Result measured() const { return measured_; }
private:
    int consecutive_ = 0;
    double last_ = 1e9;
    Result measured_;
};
}

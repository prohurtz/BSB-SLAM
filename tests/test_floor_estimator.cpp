#include "../Driver/FloorEstimator.h"
#include <stdexcept>

void require(bool condition) { if (!condition) throw std::runtime_error("floor estimator check failed"); }

int main() {
    std::vector<floor_detection::Plane> planes = {
        {1.0, {{-.5, .20, -.5}, {.5, .20, -.5}, {.5, .20, .5}, {-.5, .20, .5}}},
        {1.0, {{-.8, -.42, -.8}, {.8, -.42, -.8}, {.8, -.42, .8}, {-.8, -.42, .8}}}
    };
    auto plane_result = floor_detection::estimate_planes(planes, 1.10);
    require(plane_result.valid && plane_result.floor_y == -.42);
    floor_detection::StableFloor stable;
    require(!stable.add(plane_result));
    require(!stable.add(plane_result));
    require(stable.add(plane_result));
    require(!floor_detection::estimate_planes(planes, .15).valid);
    planes[1].normal_y = .5;
    require(!floor_detection::estimate_planes(planes, 1.10).valid);
}

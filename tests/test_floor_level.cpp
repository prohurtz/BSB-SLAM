#include "../Driver/FloorLevel.h"
#include <cmath>
#include <cstdio>
#include <stdexcept>
#include <string>

void require_at(bool ok, int line) {
    if (!ok) throw std::runtime_error("floor leveling check failed at line " + std::to_string(line));
}
#define require(ok) require_at((ok), __LINE__)

void run();
int main() {
    try { run(); } catch (const std::exception& e) { std::fprintf(stderr, "%s\n", e.what()); return 1; }
    return 0;
}
void run() {
    using namespace floor_level;
    using angular_velocity::error2;
    using angular_velocity::rotate;
    const double five = 5.0 * 3.14159265358979323846 / 180.0;

    // A 5 degree tilted "up" is rotated exactly onto +Y.
    Quat q;
    double degrees = 0;
    const Vec3 tilted{std::sin(five)*9.81, std::cos(five)*9.81, 0};
    require(rotation_to_up(tilted, 10.0, q, degrees));
    require(std::abs(degrees - 5.0) < 1e-9);
    require(error2(rotate(q, {std::sin(five), std::cos(five), 0}), {0, 1, 0}) < 1e-12);

    // Already level is the identity; too steep or degenerate input is refused.
    require(rotation_to_up({0, 9.81, 0}, 10.0, q, degrees) && degrees < 1e-9);
    require(error2(rotate(q, {1, 2, 3}), {1, 2, 3}) < 1e-12);
    require(!rotation_to_up({9.81, 0, 0}, 10.0, q, degrees));   // axis mismatch: 90 degrees
    require(!rotation_to_up({0, -9.81, 0}, 10.0, q, degrees));  // upside down
    require(!rotation_to_up({0, 0, 0}, 10.0, q, degrees));

    // Leveling pivots about the capture point, which stays put while the rest of the
    // tilted floor comes level.
    require(rotation_to_up(tilted, 10.0, q, degrees));
    const Vec3 pivot{.4, .01, -1.2};
    const Level level = compose(Level(), q, pivot);
    require(error2(level.apply(pivot), pivot) < 1e-12);
    const Vec3 tilted_floor_point{pivot.x + std::cos(five), pivot.y - std::sin(five), pivot.z};
    require(std::abs(level.apply(tilted_floor_point).y - pivot.y) < 1e-12);

    // A second capture composes with the first instead of replacing it.
    Quat small;
    require(rotation_to_up({0, std::cos(.01), std::sin(.01)}, 10.0, small, degrees));
    const Vec3 second_pivot{-.3, .01, .5};
    const Level twice = compose(level, small, second_pivot);
    require(error2(twice.apply(pivot), compose(Level(), small, second_pivot).apply(level.apply(pivot))) < 1e-12);
    require(error2(twice.apply(second_pivot), compose(Level(), small, second_pivot).apply(level.apply(second_pivot))) < 1e-12);
}

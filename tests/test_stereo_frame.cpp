#include "../Driver/StereoFrame.h"
#include <cassert>
#include <vector>

int main() {
    const uint8_t left[] = {1, 2, 3, 4};
    const uint8_t right[] = {5, 6, 7, 8};
    const std::vector<uint8_t> expected = {1, 2, 5, 6, 3, 4, 7, 8};
    assert(stereo_frame::side_by_side(left, right, 2, 2) == expected);
    assert(stereo_frame::side_by_side(nullptr, right, 2, 2).empty());
    assert(stereo_frame::side_by_side(left, right, 0, 2).empty());
}

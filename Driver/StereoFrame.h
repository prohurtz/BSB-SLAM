#pragma once
#include <cstdint>
#include <cstring>
#include <vector>

namespace stereo_frame {
// Xvisio supplies tightly packed mono8 images for the synchronized fisheye pair.
inline std::vector<uint8_t> side_by_side(const uint8_t* left, const uint8_t* right,
                                         int width, int height) {
    if (!left || !right || width <= 0 || height <= 0 || width > 4096 || height > 4096) return {};
    std::vector<uint8_t> joined(static_cast<size_t>(width) * 2 * height);
    for (int y = 0; y < height; ++y) {
        uint8_t* row = joined.data() + static_cast<size_t>(y) * width * 2;
        std::memcpy(row, left + static_cast<size_t>(y) * width, width);
        std::memcpy(row + width, right + static_cast<size_t>(y) * width, width);
    }
    return joined;
}
}

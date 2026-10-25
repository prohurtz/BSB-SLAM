#include "../Driver/CameraStream.h"
#include <winsock2.h>
#include <ws2tcpip.h>
#include <stdexcept>
#include <string>
#include <vector>

int main() {
    auto require = [](bool condition) { if (!condition) throw std::runtime_error("Camera snapshot test failed"); };
    WSADATA winsock = {};
    require(WSAStartup(MAKEWORD(2, 2), &winsock) == 0);
    CameraStream stream(0);
    require(stream.start());
    require(stream.port() > 0);
    std::vector<uint8_t> left(16 * 16, 80), right(16 * 16, 180);
    stream.publish(left.data(), right.data(), 16, 16);

    SOCKET client = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    require(client != INVALID_SOCKET);
    DWORD timeout = 3000;
    setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&timeout), sizeof(timeout));
    sockaddr_in address = {};
    address.sin_family = AF_INET;
    address.sin_port = htons(static_cast<u_short>(stream.port()));
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    require(connect(client, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0);
    const char request[] = "GET /snapshot.jpg?frame=1 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n";
    require(send(client, request, sizeof(request) - 1, 0) == sizeof(request) - 1);
    std::string response;
    char buffer[4096];
    int count = 0;
    while ((count = recv(client, buffer, sizeof(buffer), 0)) > 0)
        response.append(buffer, count);
    closesocket(client);
    stream.stop();
    WSACleanup();
    require(response.find("HTTP/1.1 200 OK") == 0);
    require(response.find("Content-Type: image/jpeg") != std::string::npos);
    const auto body = response.find("\r\n\r\n");
    require(body != std::string::npos && response.size() > body + 6);
    require(static_cast<unsigned char>(response[body + 4]) == 0xff &&
            static_cast<unsigned char>(response[body + 5]) == 0xd8);
}

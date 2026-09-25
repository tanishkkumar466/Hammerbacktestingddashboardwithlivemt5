#pragma once

#include <string>

// Discover terminal64.exe via HKCU MetaQuotes keys, then common Program Files paths.
std::string DiscoverMt5ExecutablePath();

// True if path exists and looks like terminal64.exe / terminal.exe
bool FileLooksLikeMt5Exe(const std::string& path);

# Ticket 0001: CMake Project Configuration with Presets

## Status
- [x] Draft
- [ ] C++ Design
- [ ] C++ Design Review
- [ ] C++ Test Writing
- [ ] C++ Implementation
- [ ] C++ Quality Gate
- [ ] Integration Test
- [ ] Implementation Review
- [ ] Documentation
- [ ] Merged / Complete

**Current Phase**: Draft
**Type**: Feature
**Priority**: Critical
**Assignee**: TBD
**Created**: 2026-03-02
**Estimated Complexity**: Medium
**Target Component(s)**: Build System
**Languages**: C++, CMake
**Requires Math Design**: No
**Generate Tutorial**: No
**Parent Ticket**: None
**Blocks**: [0002_gui_window_creation](0002_gui_window_creation.md), [0003_calculator_ui_components](0003_calculator_ui_components.md), [0004_unit_tests_gtest](0004_unit_tests_gtest.md)

---

## Summary

Set up the CMake build system for the Calculator GUI application. This includes the top-level `CMakeLists.txt`, CMake presets for Debug, Release, and Test configurations, dependency management for the GUI toolkit (Qt6 or Dear ImGui via SDL2), and the foundational project structure. All subsequent tickets depend on this configuration being in place.

---

## Requirements

### R1: Top-Level CMakeLists.txt
- Set minimum CMake version to 3.25 (required for presets workflow support)
- Define project name `Calculator` with `CXX` language
- Set C++17 as the required standard (`CMAKE_CXX_STANDARD 17`)
- Enable `CMAKE_CXX_STANDARD_REQUIRED ON` and `CMAKE_CXX_EXTENSIONS OFF`
- Configure output directories: `CMAKE_RUNTIME_OUTPUT_DIRECTORY`, `CMAKE_LIBRARY_OUTPUT_DIRECTORY`

### R2: CMake Presets
- Create `CMakePresets.json` at the project root with the following presets:
  - **Configure Presets**:
    - `debug` — Debug build, build directory `build/debug`
    - `release` — Release build with optimizations, build directory `build/release`
    - `test` — Debug build with coverage flags (`--coverage` or `-fprofile-arcs -ftest-coverage`), build directory `build/test`
  - **Build Presets**:
    - `debug` — Inherits from debug configure preset
    - `release` — Inherits from release configure preset
    - `test` — Inherits from test configure preset
  - **Test Presets**:
    - `test` — Runs CTest with `--output-on-failure`, inherits from test configure preset

### R3: Dependency Management
- Use `find_package()` for the chosen GUI framework
- Use `FetchContent` to pull GTest (v1.14+) for the test configuration
- Guard test dependencies with `BUILD_TESTING` option (default ON)

### R4: Project Directory Structure
- Create the following directory layout:
  ```
  calculator/
  ├── CMakeLists.txt
  ├── CMakePresets.json
  ├── src/
  │   ├── CMakeLists.txt
  │   └── main.cpp          (minimal entry point, empty main)
  ├── include/
  │   └── calculator/
  └── tests/
      └── CMakeLists.txt
  ```
- `src/CMakeLists.txt` defines the `calculator` executable target
- `tests/CMakeLists.txt` is included conditionally via `BUILD_TESTING`

### R5: Verify Build Toolchain
- The project must configure and build cleanly with each preset:
  - `cmake --preset debug && cmake --build --preset debug`
  - `cmake --preset release && cmake --build --preset release`
  - `cmake --preset test && cmake --build --preset test`
- Zero warnings with `-Wall -Wextra -Wpedantic`

---

## Acceptance Criteria

1. [ ] **AC1**: `CMakeLists.txt` exists at project root with C++17 standard enforced
2. [ ] **AC2**: `CMakePresets.json` defines debug, release, and test configure/build/test presets
3. [ ] **AC3**: Test preset enables coverage flags (`--coverage` or equivalent)
4. [ ] **AC4**: GTest is fetched via `FetchContent` and available under the test configuration
5. [ ] **AC5**: Project configures and builds with zero warnings for all three presets
6. [ ] **AC6**: Directory structure matches R4 specification
7. [ ] **AC7**: `cmake --preset test && ctest --preset test` executes successfully (even with no tests yet)

---

## Files

### New Files
- `CMakeLists.txt` — Top-level build configuration
- `CMakePresets.json` — CMake preset definitions
- `src/CMakeLists.txt` — Source target definitions
- `src/main.cpp` — Minimal application entry point
- `tests/CMakeLists.txt` — Test target definitions

---

## References
- Blocks 0002, 0003, 0004

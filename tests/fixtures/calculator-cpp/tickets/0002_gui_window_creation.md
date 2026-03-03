# Ticket 0002: GUI Window Creation

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
**Priority**: High
**Assignee**: TBD
**Created**: 2026-03-02
**Estimated Complexity**: Medium
**Target Component(s)**: GUI, Application
**Languages**: C++
**Requires Math Design**: No
**Generate Tutorial**: No
**Parent Ticket**: None
**Blocks**: [0003_calculator_ui_components](0003_calculator_ui_components.md)

---

## Summary

Create the main application window for the Calculator using a cross-platform GUI toolkit. The window should have a fixed size appropriate for a calculator layout, a title bar reading "Calculator", and a clean application lifecycle (initialize, run event loop, teardown). The architecture must cleanly separate the windowing/rendering layer from the calculator logic to support testability.

---

## Requirements

### R1: Application Class
- Create an `Application` class in `include/calculator/Application.h` and `src/Application.cpp`
- Responsibilities:
  - Initialize the GUI framework (window, renderer/context)
  - Run the main event loop
  - Handle graceful shutdown (close button, OS quit signals)
- Public interface:
  - `Application()` — constructor, initializes framework
  - `~Application()` — destructor, tears down framework
  - `int run()` — enters the main loop, returns exit code
  - `bool isRunning() const` — returns whether the main loop is active

### R2: Window Configuration
- Window title: `"Calculator"`
- Window dimensions: 320 x 480 pixels (portrait calculator layout)
- Window should NOT be resizable
- Window should be centered on screen at launch

### R3: Main Loop Structure
- The main loop must:
  1. Poll/process OS events (keyboard, mouse, window close)
  2. Handle the quit event to break the loop
  3. Clear the rendering surface each frame
  4. (Placeholder) Render UI — this is where ticket 0003 hooks in
  5. Present/swap the frame buffer
- Target a reasonable frame rate (vsync or 60 FPS cap)

### R4: Clean Separation of Concerns
- The `Application` class must NOT contain calculator logic
- Define a `Calculator` class stub in `include/calculator/Calculator.h` and `src/Calculator.cpp`
  - This will hold the computation engine (populated in ticket 0003)
  - For now: default constructor/destructor only
- `Application` owns a `Calculator` instance by composition

### R5: Update main.cpp
- `main()` creates an `Application` instance and calls `run()`
- Return the exit code from `run()`

---

## Acceptance Criteria

1. [ ] **AC1**: Application launches and displays a window titled "Calculator"
2. [ ] **AC2**: Window is 320x480 pixels and not resizable
3. [ ] **AC3**: Closing the window (X button) cleanly exits the application with code 0
4. [ ] **AC4**: `Application` class is separate from `Calculator` class
5. [ ] **AC5**: `Calculator` class stub exists with default constructor/destructor
6. [ ] **AC6**: No memory leaks or resource leaks on shutdown (validated via ASAN or manual review)
7. [ ] **AC7**: Builds cleanly with all three CMake presets from ticket 0001

---

## Files

### New Files
- `include/calculator/Application.h` — Application class declaration
- `src/Application.cpp` — Application class implementation
- `include/calculator/Calculator.h` — Calculator class stub declaration
- `src/Calculator.cpp` — Calculator class stub implementation

### Modified Files
- `src/main.cpp` — Updated to instantiate Application and call run()
- `src/CMakeLists.txt` — Add new source files to calculator target

---

## References
- Ticket 0001 (dependency — build system must be in place)
- Blocks 0003

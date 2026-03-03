# Ticket 0003: Calculator UI Components and Logic

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
**Estimated Complexity**: Large
**Target Component(s)**: GUI, Calculator Logic
**Languages**: C++
**Requires Math Design**: No
**Generate Tutorial**: No
**Parent Ticket**: None
**Blocks**: [0004_unit_tests_gtest](0004_unit_tests_gtest.md)

---

## Summary

Build the full calculator user interface and computation logic. This includes a display screen showing input and results, a number pad (0–9 with decimal point), arithmetic operation buttons (+, −, ×, ÷), an Enter/equals button to evaluate, and a Clear button to reset. The calculator logic must be fully decoupled from the GUI rendering so that the `Calculator` class can be tested independently without any GUI dependencies.

---

## Requirements

### R1: Calculator Display (Output Screen)
- A text display area at the top of the window showing the current input or result
- Display characteristics:
  - Right-aligned text
  - Monospace or fixed-width font
  - Large enough to show at least 12 characters
  - Shows `"0"` on initial state and after clear
- The display must reflect the current state of the `Calculator` model at all times

### R2: Button Layout
- Arrange buttons in a grid below the display:
  ```
  ┌─────────────────────────┐
  │           0.00          │  ← Display
  ├──────┬──────┬──────┬────┤
  │  7   │  8   │  9   │  ÷ │
  ├──────┼──────┼──────┼────┤
  │  4   │  5   │  6   │  × │
  ├──────┼──────┼──────┼────┤
  │  1   │  2   │  3   │  − │
  ├──────┼──────┼──────┼────┤
  │  C   │  0   │  .   │  + │
  ├──────┴──────┴──────┼────┤
  │                    │  = │
  └────────────────────┴────┘
  ```
- Each button must have a visible label and respond to mouse clicks
- Buttons should have visual feedback on hover/press (color change or similar)

### R3: Calculator Logic (Calculator Class)
- Expand the `Calculator` class from ticket 0002 with the following public interface:
  - `void pressDigit(int digit)` — Append digit (0–9) to current input
  - `void pressDecimal()` — Append decimal point (no-op if already present)
  - `void pressOperator(Operator op)` — Set pending operation (+, −, ×, ÷)
  - `void pressEquals()` — Evaluate the pending operation
  - `void pressClear()` — Reset to initial state
  - `std::string getDisplay() const` — Return the current display string
- Define `enum class Operator { Add, Subtract, Multiply, Divide }`
- Internal state:
  - `currentValue_` — the accumulated result (double)
  - `inputBuffer_` — the string being entered
  - `pendingOperator_` — the operator waiting to be applied
  - `hasDecimal_` — whether the input buffer contains a decimal
  - `newInput_` — flag indicating the next digit should start a new number

### R4: Arithmetic Behavior
- **Digit entry**: Appending digits builds a number string. Leading zeros are suppressed (except `"0."`)
- **Operator chaining**: Pressing an operator after a previous operator evaluates the pending expression first (e.g., `3 + 5 × ` evaluates `3 + 5 = 8`, then sets `×` as pending)
- **Equals**: Applies the pending operator to `currentValue_` and the input buffer value. Result becomes the new `currentValue_` and is displayed
- **Clear**: Resets `currentValue_` to 0, clears `inputBuffer_`, clears `pendingOperator_`, display shows `"0"`
- **Division by zero**: Display `"Error"` and require Clear before further input
- **Decimal precision**: Display up to 10 significant digits; strip trailing zeros after decimal point

### R5: Keyboard Input Support
- Map keyboard keys to calculator actions:
  - `0–9` → `pressDigit()`
  - `.` → `pressDecimal()`
  - `+`, `-`, `*`, `/` → `pressOperator()`
  - `Enter` or `=` → `pressEquals()`
  - `Escape` or `c`/`C` → `pressClear()`

### R6: Wire UI to Calculator Model
- The `Application` class owns the `Calculator` instance
- Each button click or keyboard event calls the corresponding `Calculator` method
- After each action, the display re-reads `Calculator::getDisplay()` to update

---

## Acceptance Criteria

1. [ ] **AC1**: Display shows `"0"` on application launch
2. [ ] **AC2**: Pressing digit buttons updates the display with the entered number
3. [ ] **AC3**: Pressing an operator followed by another number and equals produces the correct result
4. [ ] **AC4**: All four operations (+, −, ×, ÷) produce mathematically correct results
5. [ ] **AC5**: Division by zero displays `"Error"`
6. [ ] **AC6**: Clear resets the display to `"0"` and allows new input
7. [ ] **AC7**: Operator chaining evaluates intermediate results (e.g., `2 + 3 + ` shows `5`)
8. [ ] **AC8**: Decimal point works correctly and cannot be entered twice per number
9. [ ] **AC9**: Keyboard input works for all mapped keys
10. [ ] **AC10**: `Calculator` class has NO dependency on any GUI headers or libraries
11. [ ] **AC11**: Builds cleanly with all three CMake presets

---

## Files

### New Files
- `include/calculator/Operator.h` — Operator enum definition

### Modified Files
- `include/calculator/Calculator.h` — Full calculator interface
- `src/Calculator.cpp` — Calculator logic implementation
- `src/Application.cpp` — Wire UI rendering and input handling to Calculator
- `include/calculator/Application.h` — Add render/input helpers if needed
- `src/CMakeLists.txt` — Add any new source files

---

## References
- Ticket 0001 (dependency — build system)
- Ticket 0002 (dependency — window and class stubs)
- Blocks 0004

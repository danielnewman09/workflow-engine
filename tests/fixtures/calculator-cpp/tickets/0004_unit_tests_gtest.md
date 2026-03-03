# Ticket 0004: Unit Tests with GTest — 100% Code Coverage

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
**Target Component(s)**: Tests, Calculator Logic
**Languages**: C++
**Requires Math Design**: No
**Generate Tutorial**: No
**Parent Ticket**: None
**Blocks**: [0005_ci_coverage_reporting](0005_ci_coverage_reporting.md)

---

## Summary

Write comprehensive unit tests for the `Calculator` class using Google Test (GTest). Tests must achieve 100% line and branch coverage of the `Calculator` class logic. The test executable is built exclusively under the `test` CMake preset and run via `ctest --preset test`. Coverage reports are generated using `gcov`/`lcov` and validated as part of the quality gate.

---

## Requirements

### R1: Test File Structure
- Create `tests/CalculatorTest.cpp` as the primary test file
- Use GTest's `TEST()` or `TEST_F()` macros (fixture recommended for shared setup)
- Test fixture `CalculatorTest` should:
  - Create a fresh `Calculator` instance in `SetUp()`
  - No teardown needed (Calculator has no external resources)

### R2: Digit Entry Tests
- Single digit entry: press `5` → display `"5"`
- Multi-digit entry: press `1`, `2`, `3` → display `"123"`
- Leading zero suppression: press `0`, `5` → display `"5"`
- Zero entry: press `0` → display `"0"`
- All digits 0–9 individually verified

### R3: Decimal Point Tests
- Decimal entry: press `3`, `.`, `1`, `4` → display `"3.14"`
- Leading decimal: press `.`, `5` → display `"0.5"`
- Double decimal ignored: press `1`, `.`, `.`, `2` → display `"1.2"`
- Decimal after operator: press `5`, `+`, `.`, `3` → second operand `"0.3"`

### R4: Arithmetic Operation Tests
- **Addition**: `2 + 3 = ` → `"5"`
- **Subtraction**: `10 - 4 = ` → `"6"`
- **Multiplication**: `6 × 7 = ` → `"42"`
- **Division**: `15 ÷ 4 = ` → `"3.75"`
- **Negative results**: `3 - 8 = ` → `"-5"`
- **Floating point**: `1 ÷ 3 = ` → verify reasonable precision
- **Large numbers**: `999999999 + 1 = ` → `"1000000000"`

### R5: Operator Chaining Tests
- Chain: `2 + 3 + ` → display shows `"5"`, then `4 =` → `"9"`
- Mixed operators: `10 + 5 × ` → display `"15"`, then `2 =` → `"30"`
- Operator replacement: `5 + - 3 =` → last operator wins → `"2"`

### R6: Division by Zero Tests
- `5 ÷ 0 = ` → display `"Error"`
- After error, digit presses are ignored until Clear
- After error, operator presses are ignored until Clear
- Clear after error → display `"0"`, calculator fully functional

### R7: Clear Tests
- Clear resets display to `"0"`
- Clear mid-entry: `1`, `2`, `3`, `C` → `"0"`
- Clear after result: `2 + 3 =`, `C` → `"0"`
- Clear after error: error state, `C` → `"0"`, can enter digits

### R8: Equals Edge Cases
- Equals with no operator: press `5`, `=` → display `"5"` (no-op)
- Equals with operator but no second operand: `5 + =` → `"10"` (uses first operand as second) OR `"5"` (no-op) — document chosen behavior
- Multiple equals: `2 + 3 =` → `"5"`, `=` → behavior documented

### R9: Display Formatting Tests
- Trailing zero stripping: `1.10` → `"1.1"`
- Integer results display without decimal: `2 + 3 =` → `"5"` (not `"5.0"`)
- Large results within display width

### R10: Coverage Configuration
- Add a CMake target or script to generate coverage reports:
  - Build with `--preset test` (coverage flags already set in ticket 0001)
  - Run tests via `ctest --preset test`
  - Run `lcov` / `gcov` to collect coverage data
  - Generate HTML report via `genhtml` to `build/test/coverage/`
- Exclude test files themselves and third-party code (GTest) from coverage
- Validate: `Calculator.cpp` and `Calculator.h` must have 100% line coverage

---

## Acceptance Criteria

1. [ ] **AC1**: `tests/CalculatorTest.cpp` exists and compiles under the test preset
2. [ ] **AC2**: All tests pass when run via `ctest --preset test`
3. [ ] **AC3**: Digit entry is tested for all digits 0–9 individually
4. [ ] **AC4**: All four arithmetic operations are tested with at least 2 cases each
5. [ ] **AC5**: Division by zero produces `"Error"` and blocks further input until Clear
6. [ ] **AC6**: Operator chaining is tested with at least 2 chain scenarios
7. [ ] **AC7**: Clear resets from every state (mid-entry, post-result, post-error)
8. [ ] **AC8**: Decimal point behavior is tested (entry, leading decimal, duplicate rejection)
9. [ ] **AC9**: Coverage report can be generated via the test preset toolchain
10. [ ] **AC10**: `Calculator.cpp` achieves 100% line coverage
11. [ ] **AC11**: `Calculator.h` achieves 100% line coverage (all inline methods exercised)

---

## Files

### New Files
- `tests/CalculatorTest.cpp` — GTest test suite for Calculator class
- `cmake/Coverage.cmake` — CMake module for coverage report generation (optional, may inline in tests/CMakeLists.txt)

### Modified Files
- `tests/CMakeLists.txt` — Add CalculatorTest target, link GTest, register with CTest
- `CMakePresets.json` — Add coverage report generation step if needed

---

## References
- Ticket 0001 (dependency — build system and test preset)
- Ticket 0003 (dependency — Calculator class must be implemented)
- Blocks 0005

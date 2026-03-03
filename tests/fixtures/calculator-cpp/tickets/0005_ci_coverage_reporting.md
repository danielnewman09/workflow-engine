# Ticket 0005: CI Integration and Coverage Reporting

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
**Priority**: Medium
**Assignee**: TBD
**Created**: 2026-03-02
**Estimated Complexity**: Medium
**Target Component(s)**: CI/CD, Build System
**Languages**: C++, CMake, YAML
**Requires Math Design**: No
**Generate Tutorial**: No
**Parent Ticket**: None
**Blocks**: None

---

## Summary

Create a GitHub Actions CI pipeline that builds the calculator project with all three CMake presets, runs the test suite, generates a coverage report, and enforces the 100% coverage gate on the `Calculator` class. This provides the traceability loop: ticket → code → test → coverage → CI verification, ensuring the project can serve as a traceable reference implementation.

---

## Requirements

### R1: GitHub Actions Workflow
- Create `.github/workflows/ci.yml`
- Trigger on: `push` to `main`, `pull_request` targeting `main`
- Use Ubuntu latest runner
- Install dependencies: `cmake`, `g++`, `lcov`, GUI framework dev packages

### R2: Build Matrix
- Build all three presets in the pipeline:
  - `cmake --preset debug && cmake --build --preset debug`
  - `cmake --preset release && cmake --build --preset release`
  - `cmake --preset test && cmake --build --preset test`
- All builds must succeed with zero warnings (`-Werror` in CI)

### R3: Test Execution
- Run `ctest --preset test` after building the test preset
- Report test results using a GitHub Actions test reporter (e.g., JUnit XML output via GTest flag `--gtest_output=xml:`)
- Fail the pipeline if any test fails

### R4: Coverage Enforcement
- After test execution, run lcov/gcov to collect coverage
- Generate HTML coverage report as a build artifact
- Extract line coverage for `src/Calculator.cpp` and `include/calculator/Calculator.h`
- **Fail the pipeline** if either file is below 100% line coverage
- Upload coverage report as a GitHub Actions artifact

### R5: Traceability Artifact
- Generate a summary file `build/test/traceability-summary.txt` containing:
  - Commit SHA
  - Ticket references found in commit messages
  - Test count (pass/fail/skip)
  - Coverage percentage per source file
- Upload as a build artifact alongside the coverage report

---

## Acceptance Criteria

1. [ ] **AC1**: `.github/workflows/ci.yml` exists and is valid YAML
2. [ ] **AC2**: CI triggers on push to main and pull requests to main
3. [ ] **AC3**: All three presets build successfully in CI
4. [ ] **AC4**: Tests execute and results are reported in the CI job summary
5. [ ] **AC5**: Coverage report is generated and uploaded as a build artifact
6. [ ] **AC6**: Pipeline fails if `Calculator.cpp` line coverage drops below 100%
7. [ ] **AC7**: Traceability summary artifact is generated with commit SHA, test counts, and coverage data

---

## Files

### New Files
- `.github/workflows/ci.yml` — GitHub Actions CI pipeline
- `scripts/check-coverage.sh` — Script to parse lcov output and enforce coverage thresholds
- `scripts/traceability-summary.sh` — Script to generate the traceability summary

### Modified Files
- `CMakePresets.json` — Add CI-specific preset if needed (e.g., `-Werror` flag)
- `tests/CMakeLists.txt` — Ensure JUnit XML output is configured for GTest

---

## References
- Ticket 0001 (dependency — CMake presets)
- Ticket 0004 (dependency — tests and coverage infrastructure)

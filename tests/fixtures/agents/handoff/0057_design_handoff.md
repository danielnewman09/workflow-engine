# Design Handoff: Ticket 0057 - Contact Tangent Recording

## Agent: cpp-architect

## Mode: Initial Design (Mode 1)

## Context

**Ticket Location**: `/Users/danielnewman/Documents/GitHub/MSD-CPP/tickets/0057_contact_tangent_recording.md`

**Ticket Status**: Draft → Ready for Design

**Feature Name**: contact-tangent-recording

**Branch Name**: 0057-contact-tangent-recording

## Requirements Summary

Record contact tangent vectors (t1, t2) from the constraint solver into the simulation recording database for visualization as arrows in the Three.js replay viewer.

### Current State
- `CollisionResult` stores `normal`, `penetrationDepth`, `contacts[4]`, `contactCount`
- `FrictionConstraint` computes `tangent1_`, `tangent2_` via `TangentBasis::computeTangentBasis(normal)` with public getters
- Tangent vectors are discarded after constraint solving — not exposed through `CollisionPipeline` or recorded by `DataRecorder`

### Key Design Challenges
1. **Where to store tangent vectors**: On `CollisionResult` (geometry-level) vs. on `CollisionPair` (pipeline-level) vs. new struct
2. **When to compute**: At collision detection time (deterministic from normal) vs. extracted from `FrictionConstraint` after constraint creation
3. **Granularity**: Per-collision (shared tangent frame) vs. per-contact-point
4. **Transfer record design**: Extend `CollisionResultRecord` with tangent fields vs. new `ContactFrameRecord`

### Important Context
- The tangent basis is deterministic from the normal — `computeTangentBasis()` always produces the same t1/t2 for a given normal
- One valid approach: compute tangents at recording time (or read time) rather than threading through constraint pipeline
- However, if the solver ever uses non-deterministic tangent selection (e.g., warm-started tangent frames), recording actual solver tangents would be more accurate
- The designer should evaluate both approaches

## Requirements

### R1: Expose Tangent Vectors from Pipeline
- After constraint creation, tangent vectors (t1, t2) must be accessible for recording
- Must be associated with the correct collision pair (body A, body B)

### R2: Transfer Record for Tangent Vectors
- `CollisionResultRecord` extended with `tangent1` and `tangent2` (Vector3DRecord)
- Backward compatible: older recordings without tangent fields should still load

### R3: DataRecorder Persistence
- `DataRecorder::recordCollisions()` writes tangent vectors alongside existing collision data
- No additional SQL queries — tangent data written in same buffer pass

### R4: REST API Exposure
- `/api/v1/simulations/{id}/frames/{frame_id}/state` collision objects include `tangent1` and `tangent2`
- Python bindings (`msd_reader`) expose tangent fields on collision records

### R5: Three.js Arrow Visualization
- Two arrows (t1=green, t2=blue) rendered at contact midpoint when overlay enabled
- Normal arrow (red) already planned in 0056f — this ticket adds tangent arrows

## Files to Investigate

### Existing Architecture
- `msd-transfer/src/CollisionResultRecord.hpp` — Current collision transfer record
- `msd-sim/src/Physics/Collision/CollisionPipeline.hpp` — CollisionPair structure
- `msd-sim/src/Physics/Collision/CollisionResult.hpp` — Geometry-level collision data
- `msd-sim/src/Physics/Constraints/FrictionConstraint.hpp` — Where tangents are currently computed
- `msd-sim/src/DataRecorder/DataRecorder.cpp` — recordCollisions() implementation
- `msd-sim/src/Physics/Constraints/TangentBasis.hpp` — Tangent basis computation utility

### Related Tickets
- 0056b: Collision pipeline data extraction (dependency)
- 0056f: Three.js overlays (parent ticket)

## Expected Outputs

1. `docs/designs/contact-tangent-recording/design.md` — Architectural design document
2. `docs/designs/contact-tangent-recording/contact-tangent-recording.puml` — PlantUML diagram showing component relationships

## Open Questions for Designer to Address

1. **Storage Location Trade-off**: Should tangent vectors live on `CollisionResult` (geometry-level) or `CollisionPair` (pipeline-level)?
   - CollisionResult: Tangents computed at detection time, easier to serialize, but duplicates computation if multiple constraints share a collision
   - CollisionPair: Tangents extracted from FrictionConstraint, records actual solver state, but requires pipeline modification

2. **Computation Timing**: Compute at detection time vs. extract from solver?
   - Detection time: Deterministic from normal, no pipeline changes needed, simpler implementation
   - Solver extraction: Records actual solver state, future-proof if tangent selection becomes non-deterministic

3. **Backward Compatibility**: How to handle recordings created before tangent fields existed?
   - Nullable fields in transfer record?
   - Default zero vectors?
   - Optional wrapper?

## Constraints

- Must not break existing recording playback
- No additional SQL queries (use existing buffer pass)
- Design should support both single-contact and multi-contact collision results
- Tangent vectors must be in global frame (matching collision normal frame)

## Next Steps After Design

1. Design review (design-reviewer agent)
2. Implementation (cpp-implementer agent)
3. REST API exposure (Python/FastAPI work)
4. Three.js visualization (frontend work)

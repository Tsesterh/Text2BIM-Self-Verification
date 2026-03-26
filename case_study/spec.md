REQ-001: The model contains exactly 2 distinct building levels (storeys) with non-equal elevations, and all spaces are assigned to one of these 2 storeys.
REQ-002: There are exactly 8 rooms total, with exactly 4 rooms per storey (based on IfcSpace containment in each IfcBuildingStorey).
REQ-003: Each room (IfcSpace) has a valid 3D solid representation (closed volume) and a computable net floor area > 0.
REQ-004: No room solids self-intersect; all room boundary loops are closed and non-self-intersecting in plan.
REQ-005: Rooms on Level 2 are vertically above Level 1 (their Z extents are greater than the Level 1 storey elevation), and no room spans both storeys.
REQ-006: The roof geometry is a gable: two main planar roof faces with opposite slopes meeting along a single straight ridge line; roof faces are not horizontal.
REQ-007: The roof ridge is approximately centered over the building footprint in plan (ridge line midpoint within the footprint centroid tolerance of 10% of footprint max dimension).
REQ-008: All external walls form a closed perimeter around the Level 1 spaces with no gaps > 20 mm in plan between consecutive wall endpoints.
REQ-009: Wall, slab, and roof elements have valid non-zero thickness/volume geometry; no element has negative or zero computed volume.
REQ-010: All elements’ local placements resolve without circular references and yield finite world coordinates (no NaN/Inf transforms).
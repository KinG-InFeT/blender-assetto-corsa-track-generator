#!/usr/bin/env python3
"""
One-shot initializer: generates {slug}.blend as a starting point for manual editing.

Run with:  blender --background --python scripts/init_blend.py

Creates {slug}.blend with GeoNodes-based track geometry:
  - Road:      CenterlinePolyline + RoadGen GeoNodes (live modifier, editable)
  - Curbs:     CurbGen GeoNodes referencing road boundary edge segments
  - Grass:     GrassGen GeoNodes referencing path curves
  - Walls:     bmesh (3 faces per section: outer, inner, top)
  - Ground:    Single ground plane
  - Startline: Simple mesh
  - Empties:   AC_START, AC_PIT, AC_TIME

The export pipeline (build.sh / build_cli.py) operates on the .blend directly,
using depsgraph evaluation to resolve GeoNodes into final meshes.
"""

import os
import sys
import math
import json

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GENERATOR_DIR = os.path.dirname(SCRIPT_DIR)
ROOT_DIR = os.environ.get("TRACK_ROOT", GENERATOR_DIR)
CONFIG_PATH = os.path.join(ROOT_DIR, "track_config.json")
DEFAULTS_PATH = os.path.join(GENERATOR_DIR, "defaults.json")
TEXTURES_DIR = os.path.join(ROOT_DIR, "textures")

# Load defaults from generator project, then override with track config
_defaults = {}
if os.path.isfile(DEFAULTS_PATH):
    with open(DEFAULTS_PATH) as _df:
        _defaults = json.load(_df)

_init_config = {}
if os.path.isfile(CONFIG_PATH):
    with open(CONFIG_PATH) as _cf:
        _init_config = json.load(_cf)
_slug = _init_config.get("slug", "track")

BLEND_PATH = os.path.join(ROOT_DIR, f"{_slug}.blend")
# RoadGen.blend: try track project's demo/ first, then generator's demo/
ROADGEN_PATH = os.path.join(ROOT_DIR, "demo", "RoadGen.blend")
if not os.path.isfile(ROADGEN_PATH):
    ROADGEN_PATH = os.path.join(GENERATOR_DIR, "demo", "RoadGen.blend")

try:
    import bpy
    import bmesh
    from mathutils import Euler
except ImportError:
    print("ERROR: Must run inside Blender.")
    sys.exit(1)

# Merge defaults + track config (track config wins)
_config = {}
if os.path.isfile(CONFIG_PATH):
    with open(CONFIG_PATH) as f:
        _config = json.load(f)

_def_geo = _defaults.get("geometry", {})
_geo = _config.get("geometry", {})

ROAD_WIDTH = _geo.get("road_width", _def_geo.get("road_width", 6.0))
KERB_WIDTH = _geo.get("kerb_width", _def_geo.get("kerb_width", 1.0))
KERB_HEIGHT = _geo.get("kerb_height", _def_geo.get("kerb_height", 0.05))
GRASS_WIDTH = _geo.get("grass_width", _def_geo.get("grass_width", 2.0))
WALL_HEIGHT = _geo.get("wall_height", _def_geo.get("wall_height", 1.5))
WALL_THICKNESS = _geo.get("wall_thickness", _def_geo.get("wall_thickness", 1.0))
GROUND_MARGIN = _geo.get("ground_margin", _def_geo.get("ground_margin", 10.0))

# ============================================================
# TRACK CENTERLINE
# ============================================================

_CENTERLINE_PATH = os.path.join(ROOT_DIR, "centerline.json")

sys.path.insert(0, SCRIPT_DIR)
from spline_utils import (
    interpolate_centerline, interpolate_open,
    load_centerline_v2, resample_at_distance,
)

_cl_data = load_centerline_v2(_CENTERLINE_PATH)

# Separate layers by type
_road_layer = next((l for l in _cl_data["layers"] if l["type"] == "road"), None)
_curb_layers = [l for l in _cl_data["layers"] if l["type"] == "curb"]
_wall_layers = [l for l in _cl_data["layers"] if l["type"] == "wall"]
_start_data = _cl_data.get("start")

CONTROL_POINTS = [tuple(p) for p in (_road_layer["points"] if _road_layer else [])]
print(f"Loaded {len(CONTROL_POINTS)} control points from centerline.json"
      f" ({len(_curb_layers)} curb layers, {len(_wall_layers)} wall layers)")


def compute_normals(cl):
    n = len(cl)
    norms = []
    for i in range(n):
        tx = cl[(i+1)%n][0] - cl[(i-1)%n][0]
        ty = cl[(i+1)%n][1] - cl[(i-1)%n][1]
        le = max((tx**2+ty**2)**0.5, 1e-6)
        norms.append((-ty/le, tx/le))
    return norms


def cum_distances(cl):
    d = [0.0]
    for i in range(1, len(cl)):
        dx = cl[i][0] - cl[i - 1][0]
        dy = cl[i][1] - cl[i - 1][1]
        d.append(d[-1] + (dx**2 + dy**2)**0.5)
    return d



# ============================================================
# Blender helpers
# ============================================================

def clear_scene():
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    for coll in (bpy.data.meshes, bpy.data.materials, bpy.data.images,
                 bpy.data.curves, bpy.data.node_groups):
        for b in list(coll):
            if b.users == 0:
                coll.remove(b)


def make_material(name, tex_file, ks_amb=0.5, ks_dif=0.7, ks_spec=0.2, ks_exp=15.0):
    mat = bpy.data.materials.new(name=name)
    try:
        mat.use_nodes = True
    except AttributeError:
        pass
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    for nd in list(nodes):
        nodes.remove(nd)
    out = nodes.new('ShaderNodeOutputMaterial')
    out.location = (300, 0)
    bsdf = nodes.new('ShaderNodeBsdfPrincipled')
    bsdf.location = (0, 0)
    tex_path = os.path.join(TEXTURES_DIR, tex_file)
    if os.path.isfile(tex_path):
        tn = nodes.new('ShaderNodeTexImage')
        tn.location = (-300, 0)
        tn.image = bpy.data.images.load(tex_path)
        links.new(tn.outputs['Color'], bsdf.inputs['Base Color'])
        print(f"  [OK] Texture loaded: {tex_file}")
    else:
        print(f"  [WARN] Texture NOT FOUND: {tex_path}")
    links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
    mat['ac_shader'] = 'ksPerPixel'
    mat['ksAmbient'] = ks_amb
    mat['ksDiffuse'] = ks_dif
    mat['ksSpecular'] = ks_spec
    mat['ksSpecularEXP'] = ks_exp
    return mat


def make_color_material(name, r, g, b, ks_amb=0.5, ks_dif=0.7, ks_spec=0.1, ks_exp=5.0):
    """Create a solid-color material (no texture) with AC shader properties."""
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    for nd in list(nodes):
        nodes.remove(nd)
    out = nodes.new('ShaderNodeOutputMaterial')
    out.location = (300, 0)
    bsdf = nodes.new('ShaderNodeBsdfPrincipled')
    bsdf.location = (0, 0)
    bsdf.inputs['Base Color'].default_value = (r, g, b, 1.0)
    links.new(bsdf.outputs['BSDF'], out.inputs['Surface'])
    mat['ac_shader'] = 'ksPerPixel'
    mat['ksAmbient'] = ks_amb
    mat['ksDiffuse'] = ks_dif
    mat['ksSpecular'] = ks_spec
    mat['ksSpecularEXP'] = ks_exp
    return mat


def _add_uv_projection(nodes, links, geom_socket, tile=5.0, use_z_for_v=False):
    """Add world-space UV projection via Store Named Attribute.

    For flat geometry (use_z_for_v=False): UV = (pos.x / tile, pos.y / tile)
    For vertical geometry (use_z_for_v=True): UV = ((pos.x + pos.y) / tile, pos.z / tile)

    Args:
        nodes, links: Node tree nodes/links.
        geom_socket: Output socket providing geometry (e.g., set_mat.outputs['Geometry']).
        tile: UV tiling scale (meters per UV unit).
        use_z_for_v: Use Z coordinate for V (walls) instead of Y (flat).

    Returns:
        store node — connect store.outputs['Geometry'] to the next node.
    """
    pos = nodes.new('GeometryNodeInputPosition')
    sep = nodes.new('ShaderNodeSeparateXYZ')
    links.new(pos.outputs['Position'], sep.inputs['Vector'])

    if use_z_for_v:
        add_xy = nodes.new('ShaderNodeMath')
        add_xy.operation = 'ADD'
        links.new(sep.outputs['X'], add_xy.inputs[0])
        links.new(sep.outputs['Y'], add_xy.inputs[1])

        div_u = nodes.new('ShaderNodeMath')
        div_u.operation = 'DIVIDE'
        div_u.inputs[1].default_value = tile
        links.new(add_xy.outputs[0], div_u.inputs[0])

        div_v = nodes.new('ShaderNodeMath')
        div_v.operation = 'DIVIDE'
        div_v.inputs[1].default_value = tile
        links.new(sep.outputs['Z'], div_v.inputs[0])
    else:
        div_u = nodes.new('ShaderNodeMath')
        div_u.operation = 'DIVIDE'
        div_u.inputs[1].default_value = tile
        links.new(sep.outputs['X'], div_u.inputs[0])

        div_v = nodes.new('ShaderNodeMath')
        div_v.operation = 'DIVIDE'
        div_v.inputs[1].default_value = tile
        links.new(sep.outputs['Y'], div_v.inputs[0])

    comb = nodes.new('ShaderNodeCombineXYZ')
    links.new(div_u.outputs[0], comb.inputs['X'])
    links.new(div_v.outputs[0], comb.inputs['Y'])

    store = nodes.new('GeometryNodeStoreNamedAttribute')
    store.data_type = 'FLOAT2'
    store.domain = 'CORNER'
    # Set attribute name to "UVMap"
    for inp in store.inputs:
        try:
            if isinstance(inp.default_value, str):
                inp.default_value = "UVMap"
                break
        except (AttributeError, TypeError):
            pass

    links.new(geom_socket, store.inputs['Geometry'])
    links.new(comb.outputs['Vector'], store.inputs['Value'])

    return store


# ============================================================
# Edge polyline helpers
# ============================================================

def create_edge_polyline(name, coords, cyclic=True):
    """Create an edge-only polyline mesh from coordinates.

    This is the same technique used for the road (1ROAD): each object's
    base mesh IS the editable path.  GeoNodes convert it to final geometry
    via Mesh to Curve → Curve to Mesh.

    coords: list of (x,y) or (x,y,z) tuples.
    cyclic: close the loop (last vertex → first vertex).
    """
    me = bpy.data.meshes.new(name + "_polyline")
    ob = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(ob)

    bm = bmesh.new()
    verts = []
    for pt in coords:
        if len(pt) == 2:
            verts.append(bm.verts.new((pt[0], pt[1], 0.0)))
        else:
            verts.append(bm.verts.new(pt))
    bm.verts.ensure_lookup_table()

    n = len(verts)
    for i in range(n - 1):
        bm.edges.new([verts[i], verts[i + 1]])
    if cyclic and n > 2:
        bm.edges.new([verts[-1], verts[0]])

    bm.to_mesh(me)
    bm.free()
    me.update()
    return ob


# ============================================================
# RoadGen GeoNodes — live modifier (NOT applied)
# ============================================================


def append_roadgen_geonodes():
    """Append RoadGen geometry nodes group from demo/RoadGen.blend."""
    if not os.path.isfile(ROADGEN_PATH):
        print(f"  [WARN] RoadGen.blend not found at {ROADGEN_PATH}")
        return None

    with bpy.data.libraries.load(ROADGEN_PATH, link=False) as (data_from, data_to):
        available = [n for n in data_from.node_groups]
        print(f"  [INFO] Node groups in RoadGen.blend: {available}")

        roadgen_name = None
        if "RoadGen" in available:
            roadgen_name = "RoadGen"
        else:
            for name in available:
                if "roadgen" in name.lower() or "road" in name.lower():
                    roadgen_name = name
                    break

        if roadgen_name is None and available:
            roadgen_name = available[0]
            print(f"  [INFO] No 'RoadGen' group found, using: {roadgen_name}")

        if roadgen_name:
            data_to.node_groups = [roadgen_name]

    if roadgen_name and roadgen_name in bpy.data.node_groups:
        print(f"  [OK] Appended node group: {roadgen_name}")
        return bpy.data.node_groups[roadgen_name]

    print("  [WARN] Could not append any node group from RoadGen.blend")
    return None


def verify_road_boundary_loops(road_obj):
    """Check evaluated road mesh has exactly 2 boundary loops (for AI line)."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    obj_eval = road_obj.evaluated_get(depsgraph)

    bm = bmesh.new()
    bm.from_mesh(obj_eval.data)
    bm.edges.ensure_lookup_table()

    boundary_edges = [e for e in bm.edges if e.is_boundary]
    if not boundary_edges:
        bm.free()
        print("  [INFO] Road mesh has 0 boundary loops (need 2)")
        return False

    visited = set()
    loops = 0
    for edge in boundary_edges:
        if edge.index in visited:
            continue
        loops += 1
        queue = [edge]
        while queue:
            e = queue.pop()
            if e.index in visited:
                continue
            visited.add(e.index)
            for v in e.verts:
                for linked in v.link_edges:
                    if linked.is_boundary and linked.index not in visited:
                        queue.append(linked)

    bm.free()
    print(f"  [INFO] Road mesh has {loops} boundary loop(s) (need 2)")
    return loops == 2


def create_setmaterial_nodegroup(name="SetMaterialNG"):
    """Create a simple GeoNodes group: Group Input → Set Material → Group Output.

    Used to assign material to objects whose primary GeoNodes (e.g., RoadGen)
    don't have a Set Material node.
    """
    ng = bpy.data.node_groups.new(name, 'GeometryNodeTree')

    ng.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    ng.interface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    s_mat = ng.interface.new_socket("Material", in_out='INPUT', socket_type='NodeSocketMaterial')

    nodes = ng.nodes
    links = ng.links

    gi = nodes.new('NodeGroupInput'); gi.location = (-400, 0)
    go = nodes.new('NodeGroupOutput'); go.location = (400, 0)

    # Flip faces — RoadGen produces normals pointing down, AC needs them up
    flip = nodes.new('GeometryNodeFlipFaces')
    flip.location = (-200, 0)
    links.new(gi.outputs['Geometry'], flip.inputs['Mesh'])

    set_mat = nodes.new('GeometryNodeSetMaterial')
    set_mat.location = (0, 0)
    links.new(flip.outputs['Mesh'], set_mat.inputs['Geometry'])
    links.new(gi.outputs['Material'], set_mat.inputs['Material'])

    # UV projection (world-space XY)
    store_uv = _add_uv_projection(nodes, links, set_mat.outputs['Geometry'])
    links.new(store_uv.outputs['Geometry'], go.inputs['Geometry'])

    ng['_id_mat'] = s_mat.identifier
    return ng


def build_road_with_roadgen(ctrl_pts, mat):
    """Build 1ROAD as a live RoadGen GeoNodes modifier on the polyline.

    The modifier is NOT applied — the road remains editable.
    A second modifier (SetMaterial) assigns the asphalt material since
    the external RoadGen node group doesn't include a Set Material node.
    Export pipeline uses depsgraph evaluation to get the final mesh.
    """
    print("\nBuilding 1ROAD with RoadGen GeoNodes (live)...")

    # Create polyline from control points (edge-only mesh, editable)
    road_obj = create_edge_polyline("1ROAD", ctrl_pts, cyclic=True)
    print(f"  [OK] Polyline created: {len(ctrl_pts)} control points")

    # Append RoadGen node group
    ng = append_roadgen_geonodes()
    if ng is None:
        print("  [FALLBACK] RoadGen not available, road will be plain polyline")
        return road_obj

    # Add GeoNodes modifier (keep LIVE)
    mod = road_obj.modifiers.new(name="RoadGen", type='NODES')
    mod.node_group = ng

    # Set Lane Width from config
    if hasattr(ng, 'interface') and hasattr(ng.interface, 'items_tree'):
        for item in ng.interface.items_tree:
            if hasattr(item, 'socket_type') and item.name == "Lane Width":
                mod[item.identifier] = ROAD_WIDTH
                print(f"  [OK] Set Lane Width = {ROAD_WIDTH} m")
                break
        else:
            print(f"  [WARN] 'Lane Width' input not found in {ng.name}, using default")

    # Add SetMaterial modifier (second modifier, after RoadGen)
    # Needed because external RoadGen doesn't include a Set Material node
    mat_ng = create_setmaterial_nodegroup("RoadMaterial")
    mat_mod = road_obj.modifiers.new(name="RoadMaterial", type='NODES')
    mat_mod.node_group = mat_ng
    mat_mod[mat_ng['_id_mat']] = mat
    print(f"  [OK] Added RoadMaterial modifier (mat_asphalt)")

    # Assign material to base mesh too (for viewport display)
    road_obj.data.materials.append(mat)

    # Force depsgraph update and verify boundary loops
    bpy.context.view_layer.objects.active = road_obj
    road_obj.select_set(True)
    bpy.context.view_layer.update()

    if verify_road_boundary_loops(road_obj):
        print("  [OK] Road mesh has correct boundary loops for AI line")
    else:
        print("  [WARN] Road mesh does NOT have 2 boundary loops — AI line may fail")

    return road_obj


# ============================================================
# Boundary extraction from evaluated road (for curbs)
# ============================================================

def find_nearest_index(coords, target_x, target_y):
    """Find the index in coords nearest to the target point."""
    min_dist = float('inf')
    best = 0
    for i, (x, y, z) in enumerate(coords):
        d = ((x - target_x)**2 + (y - target_y)**2)**0.5
        if d < min_dist:
            min_dist = d
            best = i
    return best


# ============================================================
# GeoNodes: CurbGen
# ============================================================

def create_curb_profile():
    """Create a shared CurbProfile curve with trapezoidal cross-section.

    Profile (viewed from the side, after Curve to Mesh mapping Y → -Z):

        ___________
       /           \\
      /             \\
    _/               \\_

    4 points: ground → bevel up → flat top → bevel down → ground.
    Profile Y is negative so it maps to positive Z (up from road).
    """
    w = KERB_WIDTH
    h = KERB_HEIGHT
    bevel = w * 0.20  # 20% bevel on each side

    curve_data = bpy.data.curves.new("CurbProfile", 'CURVE')
    curve_data.dimensions = '3D'
    spline = curve_data.splines.new('POLY')

    points = [
        (-w / 2, 0, 0),               # ground level, outer edge
        (-w / 2 + bevel, -h, 0),      # top, outer bevel (-Y → +Z)
        (w / 2 - bevel, -h, 0),       # top, inner bevel
        (w / 2, 0, 0),                # ground level, inner edge
    ]
    spline.points.add(len(points) - 1)
    for i, (x, y, z) in enumerate(points):
        spline.points[i].co = (x, y, z, 1.0)
    spline.use_cyclic_u = False

    obj = bpy.data.objects.new("CurbProfile", curve_data)
    bpy.context.collection.objects.link(obj)
    obj.hide_render = True
    obj.hide_set(True)  # hidden from viewport but still selectable in outliner

    print(f"  [OK] CurbProfile: W={w}m, H={h}m, bevel={bevel}m")
    return obj


def create_curbgen_nodegroup():
    """CurbGen: Geometry (own mesh) → Mesh to Curve → Curve to Mesh (CurbProfile) → Set Material.

    The object's base mesh IS the editable curb path (edge polyline).
    GeoNodes convert it to a curve, sweep the CurbProfile along it,
    and output the final 3D curb mesh.  Editable in Edit Mode.
    """
    ng = bpy.data.node_groups.new("CurbGen", 'GeometryNodeTree')

    ng.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    ng.interface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    s_profile = ng.interface.new_socket("Profile", in_out='INPUT', socket_type='NodeSocketObject')
    s_mat = ng.interface.new_socket("Material", in_out='INPUT', socket_type='NodeSocketMaterial')

    nodes = ng.nodes
    links = ng.links

    gi = nodes.new('NodeGroupInput'); gi.location = (-600, 0)
    go = nodes.new('NodeGroupOutput'); go.location = (600, 0)

    # Convert own edge polyline to curve
    m2c = nodes.new('GeometryNodeMeshToCurve')
    m2c.location = (-400, 0)
    links.new(gi.outputs['Geometry'], m2c.inputs['Mesh'])

    # Get profile geometry from shared CurbProfile object
    profile_info = nodes.new('GeometryNodeObjectInfo')
    profile_info.location = (-400, -200)
    profile_info.transform_space = 'RELATIVE'
    links.new(gi.outputs['Profile'], profile_info.inputs['Object'])

    # Curve to Mesh: sweep CurbProfile along curb path
    c2m = nodes.new('GeometryNodeCurveToMesh')
    c2m.location = (0, 0)
    links.new(m2c.outputs['Curve'], c2m.inputs['Curve'])
    links.new(profile_info.outputs['Geometry'], c2m.inputs['Profile Curve'])

    # Set Material
    set_mat = nodes.new('GeometryNodeSetMaterial')
    set_mat.location = (200, 0)
    links.new(c2m.outputs['Mesh'], set_mat.inputs['Geometry'])
    links.new(gi.outputs['Material'], set_mat.inputs['Material'])

    # UV projection (world-space XY)
    store_uv = _add_uv_projection(nodes, links, set_mat.outputs['Geometry'])
    links.new(store_uv.outputs['Geometry'], go.inputs['Geometry'])

    ng['_id_profile'] = s_profile.identifier
    ng['_id_mat'] = s_mat.identifier

    print("  [OK] Created CurbGen GeoNodes group")
    return ng


def build_curbs_from_layers(curb_layers, curbgen_ng, mat, profile_obj):
    """Build curb objects from v2 layout layers."""
    id_profile = curbgen_ng['_id_profile']
    id_mat = curbgen_ng['_id_mat']
    objs = []
    for layer_data in curb_layers:
        pts = layer_data["points"] if isinstance(layer_data, dict) else layer_data.points
        name = layer_data["name"] if isinstance(layer_data, dict) else layer_data.name
        closed = layer_data.get("closed", False) if isinstance(layer_data, dict) else getattr(layer_data, 'closed', False)
        if len(pts) < 2:
            continue
        spline = interpolate_centerline(pts, pts_per_seg=20) if closed else interpolate_open(pts, pts_per_seg=20)
        coords_3d = [(p[0], p[1], 0.0) for p in spline]
        # Ensure naming convention
        obj_name = name.upper()
        if not obj_name.startswith(("1KERB_", "2KERB_")):
            obj_name = f"1KERB_{obj_name}"
        ob = create_edge_polyline(obj_name, coords_3d, cyclic=closed)
        mod = ob.modifiers.new("CurbGen", 'NODES')
        mod.node_group = curbgen_ng
        mod[id_profile] = profile_obj
        mod[id_mat] = mat
        ob.data.materials.append(mat)
        objs.append(ob)
        print(f"  {obj_name}: {len(coords_3d)} pts from layer '{name}' ({'closed' if closed else 'open'})")
    return objs


def build_walls_from_layers(wall_layers, mat):
    """Build wall objects from v2 layout layers using bmesh for AC collision.

    Each wall segment has 3 faces per section (outer, inner, top) with
    explicit vertex ordering for correct normals. This matches the proven
    approach that works with AC's collision engine.
    """
    import bmesh
    seg_len = 25.0
    objs = []
    for wall_idx, layer_data in enumerate(wall_layers):
        pts = layer_data["points"] if isinstance(layer_data, dict) else layer_data.points
        name = layer_data["name"] if isinstance(layer_data, dict) else layer_data.name
        closed = layer_data.get("closed", False) if isinstance(layer_data, dict) else getattr(layer_data, 'closed', False)
        if len(pts) < 2:
            continue
        spline = interpolate_centerline(pts, pts_per_seg=20) if closed else interpolate_open(pts, pts_per_seg=20)
        prefix = f"{wall_idx + 1}WALL"
        np_ = len(spline)

        # Compute normals for the spline
        norms = []
        for i in range(np_):
            j = (i + 1) % np_
            dx = spline[j][0] - spline[i][0]
            dy = spline[j][1] - spline[i][1]
            ln = max((dx**2 + dy**2)**0.5, 1e-6)
            norms.append((-dy / ln, dx / ln))

        # Compute total length for segmentation
        total_len = 0.0
        for i in range(np_ - (0 if closed else 1)):
            j = (i + 1) % np_
            total_len += ((spline[j][0] - spline[i][0])**2 +
                          (spline[j][1] - spline[i][1])**2)**0.5
        nseg = max(1, int(total_len / seg_len))
        pps = max(2, np_ // nseg)

        half_t = WALL_THICKNESS / 2
        for si in range(nseg):
            s = si * pps
            e = min((si + 1) * pps + 1, np_)
            if si == nseg - 1:
                e = np_

            seg_name = f"{prefix}_SUB{si}"
            me = bpy.data.meshes.new(seg_name)
            ob = bpy.data.objects.new(seg_name, me)
            bpy.context.collection.objects.link(ob)

            bm = bmesh.new()
            uvl = bm.loops.layers.uv.new("UVMap")
            bi, bo, ti, to_ = [], [], [], []
            for i in range(s, e):
                idx = i % np_
                cx, cy = spline[idx]
                nx, ny = norms[idx]
                bix, biy = cx + nx * half_t, cy + ny * half_t
                box_, boy = cx - nx * half_t, cy - ny * half_t
                bi.append(bm.verts.new((bix, biy, 0)))
                bo.append(bm.verts.new((box_, boy, 0)))
                ti.append(bm.verts.new((bix, biy, WALL_HEIGHT)))
                to_.append(bm.verts.new((box_, boy, WALL_HEIGHT)))

            bm.verts.ensure_lookup_table()
            for i in range(e - s - 1):
                for verts in ([bo[i], bo[i+1], to_[i+1], to_[i]],
                              [bi[i+1], bi[i], ti[i], ti[i+1]],
                              [ti[i], to_[i], to_[i+1], ti[i+1]]):
                    try:
                        f = bm.faces.new(verts)
                        for li, lp in enumerate(f.loops):
                            lp[uvl].uv = (float(li % 2), float(li // 2))
                    except ValueError:
                        pass

            bm.normal_update()
            bm.to_mesh(me)
            bm.free()
            me.update()
            ob.data.materials.append(mat)
            objs.append(ob)

        print(f"  {prefix}: {nseg} segments from layer '{name}'")
    return objs


# ============================================================
# GeoNodes: GrassGen
# ============================================================

def create_grassgen_nodegroup():
    """GrassGen: Geometry → Mesh to Curve → Curve to Mesh (flat strip) → Set Material.

    The object's base mesh IS the editable grass path.
    Editable in Edit Mode like the road.
    """
    ng = bpy.data.node_groups.new("GrassGen", 'GeometryNodeTree')

    ng.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    ng.interface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    s_width = ng.interface.new_socket("Width", in_out='INPUT', socket_type='NodeSocketFloat')
    s_width.default_value = GRASS_WIDTH
    s_mat = ng.interface.new_socket("Material", in_out='INPUT', socket_type='NodeSocketMaterial')

    nodes = ng.nodes
    links = ng.links

    gi = nodes.new('NodeGroupInput'); gi.location = (-600, 0)
    go = nodes.new('NodeGroupOutput'); go.location = (600, 0)

    # Convert own edge polyline to curve
    m2c = nodes.new('GeometryNodeMeshToCurve')
    m2c.location = (-400, 0)
    links.new(gi.outputs['Geometry'], m2c.inputs['Mesh'])

    # Profile: flat line of Width
    half_neg = nodes.new('ShaderNodeMath')
    half_neg.location = (-400, -200)
    half_neg.operation = 'MULTIPLY'
    half_neg.inputs[1].default_value = -0.5
    links.new(gi.outputs['Width'], half_neg.inputs[0])

    half_pos = nodes.new('ShaderNodeMath')
    half_pos.location = (-400, -300)
    half_pos.operation = 'MULTIPLY'
    half_pos.inputs[1].default_value = 0.5
    links.new(gi.outputs['Width'], half_pos.inputs[0])

    combine_a = nodes.new('ShaderNodeCombineXYZ')
    combine_a.location = (-200, -200)
    links.new(half_neg.outputs[0], combine_a.inputs['X'])

    combine_b = nodes.new('ShaderNodeCombineXYZ')
    combine_b.location = (-200, -300)
    links.new(half_pos.outputs[0], combine_b.inputs['X'])

    profile_line = nodes.new('GeometryNodeCurvePrimitiveLine')
    profile_line.location = (0, -250)
    links.new(combine_a.outputs['Vector'], profile_line.inputs['Start'])
    links.new(combine_b.outputs['Vector'], profile_line.inputs['End'])

    # Curve to Mesh
    c2m = nodes.new('GeometryNodeCurveToMesh')
    c2m.location = (0, 0)
    links.new(m2c.outputs['Curve'], c2m.inputs['Curve'])
    links.new(profile_line.outputs['Curve'], c2m.inputs['Profile Curve'])

    # Set Material
    set_mat = nodes.new('GeometryNodeSetMaterial')
    set_mat.location = (200, 0)
    links.new(c2m.outputs['Mesh'], set_mat.inputs['Geometry'])
    links.new(gi.outputs['Material'], set_mat.inputs['Material'])

    # UV projection (world-space XY)
    store_uv = _add_uv_projection(nodes, links, set_mat.outputs['Geometry'])
    links.new(store_uv.outputs['Geometry'], go.inputs['Geometry'])

    ng['_id_width'] = s_width.identifier
    ng['_id_mat'] = s_mat.identifier

    print("  [OK] Created GrassGen GeoNodes group")
    return ng


def build_grass_geonodes(cl, norms, hw, grassgen_ng, mat):
    """Create 1GRASS and 2GRASS as edge polylines with GrassGen modifiers.

    Each grass object's base mesh IS its editable path.
    Select → Edit Mode → edit control points directly.
    """
    id_width = grassgen_ng['_id_width']
    id_mat = grassgen_ng['_id_mat']

    objs = []
    for name, offset_sign in [("1GRASS", 1), ("2GRASS", -1)]:
        offset = offset_sign * (hw + GRASS_WIDTH / 2)
        # Compute path coordinates directly
        coords = [(cl[i][0] + norms[i][0] * offset,
                    cl[i][1] + norms[i][1] * offset,
                    -0.01) for i in range(len(cl))]

        ob = create_edge_polyline(name, coords, cyclic=True)

        mod = ob.modifiers.new("GrassGen", 'NODES')
        mod.node_group = grassgen_ng
        mod[id_width] = GRASS_WIDTH
        mod[id_mat] = mat

        ob.data.materials.append(mat)
        objs.append(ob)
        print(f"  {name}: offset={offset:.1f}m, width={GRASS_WIDTH}m, {len(coords)} pts")

    return objs


# ============================================================
# Ground (single plane)
# ============================================================

def build_ground(cl, hw, mat):
    """Single ground plane covering the track area."""
    xs = [c[0] for c in cl]
    ys = [c[1] for c in cl]
    mg = hw + GRASS_WIDTH + WALL_THICKNESS + GROUND_MARGIN
    x0 = min(xs) - mg
    x1 = max(xs) + mg
    y0 = min(ys) - mg
    y1 = max(ys) + mg

    # Subdivide ground into tiles for repeating texture
    tile = 10.0  # meters per UV repeat
    nx_tiles = max(1, int(math.ceil((x1 - x0) / tile)))
    ny_tiles = max(1, int(math.ceil((y1 - y0) / tile)))
    # Snap extents to exact tile boundaries
    x1 = x0 + nx_tiles * tile
    y1 = y0 + ny_tiles * tile

    me = bpy.data.meshes.new("1GROUND")
    ob = bpy.data.objects.new("1GROUND", me)
    bpy.context.collection.objects.link(ob)
    bm = bmesh.new()
    uvl = bm.loops.layers.uv.new("UVMap")

    # Create grid of tile-sized quads
    verts_grid = []
    for iy in range(ny_tiles + 1):
        row = []
        for ix in range(nx_tiles + 1):
            v = bm.verts.new((x0 + ix * tile, y0 + iy * tile, -0.05))
            row.append(v)
        verts_grid.append(row)

    for iy in range(ny_tiles):
        for ix in range(nx_tiles):
            v0 = verts_grid[iy][ix]
            v1 = verts_grid[iy][ix + 1]
            v2 = verts_grid[iy + 1][ix + 1]
            v3 = verts_grid[iy + 1][ix]
            f = bm.faces.new([v0, v1, v2, v3])
            f.loops[0][uvl].uv = (0, 0)
            f.loops[1][uvl].uv = (1, 0)
            f.loops[2][uvl].uv = (1, 1)
            f.loops[3][uvl].uv = (0, 1)

    bm.normal_update()
    bm.to_mesh(me)
    bm.free()
    me.update()
    ob.data.materials.append(mat)

    print(f"  [OK] Ground plane: {x1-x0:.0f}x{y1-y0:.0f}m ({nx_tiles}x{ny_tiles} tiles @ {tile}m)")
    return ob


# ============================================================
# Startline
# ============================================================

def build_startline(cl, norms, start_idx, mat):
    """Build startline mesh across the road at the given centerline index."""
    n = len(cl)
    idx = start_idx % n
    cx, cy = cl[idx]
    nx, ny = norms[idx]
    j = (idx + 1) % n
    tx = cl[j][0] - cx
    ty = cl[j][1] - cy
    tl2 = max((tx**2 + ty**2)**0.5, 1e-6)
    tx /= tl2
    ty /= tl2
    line_len = 2.0
    hw_r = ROAD_WIDTH / 2
    hl = line_len / 2
    nm = "1STARTLINE"
    me = bpy.data.meshes.new(nm)
    ob = bpy.data.objects.new(nm, me)
    bpy.context.collection.objects.link(ob)
    bm = bmesh.new()
    uvl = bm.loops.layers.uv.new("UVMap")
    v0 = bm.verts.new((cx - nx * hw_r - tx * hl, cy - ny * hw_r - ty * hl, 0.005))
    v1 = bm.verts.new((cx + nx * hw_r - tx * hl, cy + ny * hw_r - ty * hl, 0.005))
    v2 = bm.verts.new((cx + nx * hw_r + tx * hl, cy + ny * hw_r + ty * hl, 0.005))
    v3 = bm.verts.new((cx - nx * hw_r + tx * hl, cy - ny * hw_r + ty * hl, 0.005))
    f = bm.faces.new([v0, v1, v2, v3])
    f.loops[0][uvl].uv = (0, 0)
    f.loops[1][uvl].uv = (1, 0)
    f.loops[2][uvl].uv = (1, 1)
    f.loops[3][uvl].uv = (0, 1)
    bm.normal_update()
    for face in bm.faces:
        if face.normal.z < 0:
            face.normal_flip()
    bm.to_mesh(me)
    bm.free()
    me.update()
    ob.data.materials.append(mat)
    return ob


def build_start_gantry(cl, norms, start_idx, mat_struct, mat_sponsor, mat_light):
    """Build a start gantry (portal) over the startline with sponsor panel and lights."""
    n = len(cl)
    idx = start_idx % n
    cx, cy = cl[idx]
    nx_n, ny_n = norms[idx]
    j = (idx + 1) % n
    tx = cl[j][0] - cx
    ty = cl[j][1] - cy
    tl2 = max((tx**2 + ty**2)**0.5, 1e-6)
    tx /= tl2
    ty /= tl2

    hw = ROAD_WIDTH / 2 + 0.3  # pillar offset from center
    pillar_s = 0.15             # pillar cross-section
    pillar_h = 4.5              # pillar height
    beam_s = 0.15               # beam cross-section
    panel_w = 3.0               # sponsor panel width
    panel_h = 1.2               # sponsor panel height
    panel_z_bot = 3.2           # panel bottom z
    light_r = 0.08              # light disc radius
    light_z = 3.0               # lights z position
    light_seg = 8               # segments per light disc
    n_lights = 5
    light_spread = 2.4          # total width of light row

    nm = "1GANTRY"
    me = bpy.data.meshes.new(nm)
    ob = bpy.data.objects.new(nm, me)
    bpy.context.collection.objects.link(ob)
    bm = bmesh.new()
    uvl = bm.loops.layers.uv.new("UVMap")

    def _box(cx_b, cy_b, cz_b, sx, sy, sz, mat_idx):
        """Add an axis-aligned box centered at (cx_b, cy_b, cz_b) with half-sizes."""
        # Transform local axes: normal=X, tangent=Y in world
        hsx, hsy, hsz = sx / 2, sy / 2, sz / 2
        corners = []
        for dz in (-hsz, hsz):
            for dn in (-hsx, hsx):
                for dt in (-hsy, hsy):
                    wx = cx_b + nx_n * dn + tx * dt
                    wy = cy_b + ny_n * dn + ty * dt
                    wz = cz_b + dz
                    corners.append(bm.verts.new((wx, wy, wz)))
        # 6 faces: bottom, top, front, back, left, right
        faces_idx = [
            (0, 1, 3, 2),  # bottom
            (4, 6, 7, 5),  # top
            (0, 4, 5, 1),  # front
            (2, 3, 7, 6),  # back
            (0, 2, 6, 4),  # left
            (1, 5, 7, 3),  # right
        ]
        for fi in faces_idx:
            f = bm.faces.new([corners[i] for i in fi])
            f.material_index = mat_idx

    # Left pillar
    lx = cx - nx_n * hw
    ly = cy - ny_n * hw
    _box(lx, ly, pillar_h / 2, pillar_s, pillar_s, pillar_h, 0)

    # Right pillar
    rx = cx + nx_n * hw
    ry = cy + ny_n * hw
    _box(rx, ry, pillar_h / 2, pillar_s, pillar_s, pillar_h, 0)

    # Top beam connecting pillars
    beam_cx = cx
    beam_cy = cy
    beam_len = hw * 2
    _box(beam_cx, beam_cy, pillar_h - beam_s / 2, beam_len, beam_s, beam_s, 0)

    # Sponsor panel (single quad facing the driving direction)
    panel_z_top = panel_z_bot + panel_h
    hp = panel_w / 2
    p0 = bm.verts.new((cx - nx_n * hp - tx * 0.01, cy - ny_n * hp - ty * 0.01, panel_z_bot))
    p1 = bm.verts.new((cx + nx_n * hp - tx * 0.01, cy + ny_n * hp - ty * 0.01, panel_z_bot))
    p2 = bm.verts.new((cx + nx_n * hp - tx * 0.01, cy + ny_n * hp - ty * 0.01, panel_z_top))
    p3 = bm.verts.new((cx - nx_n * hp - tx * 0.01, cy - ny_n * hp - ty * 0.01, panel_z_top))
    pf = bm.faces.new([p0, p1, p2, p3])
    pf.material_index = 1
    # UV mapping for sponsor texture
    pf.loops[0][uvl].uv = (0, 0)
    pf.loops[1][uvl].uv = (1, 0)
    pf.loops[2][uvl].uv = (1, 1)
    pf.loops[3][uvl].uv = (0, 1)

    # Back face of panel (visible from the other side)
    pb0 = bm.verts.new((cx - nx_n * hp + tx * 0.01, cy - ny_n * hp + ty * 0.01, panel_z_bot))
    pb1 = bm.verts.new((cx + nx_n * hp + tx * 0.01, cy + ny_n * hp + ty * 0.01, panel_z_bot))
    pb2 = bm.verts.new((cx + nx_n * hp + tx * 0.01, cy + ny_n * hp + ty * 0.01, panel_z_top))
    pb3 = bm.verts.new((cx - nx_n * hp + tx * 0.01, cy - ny_n * hp + ty * 0.01, panel_z_top))
    pbf = bm.faces.new([pb1, pb0, pb3, pb2])
    pbf.material_index = 1
    pbf.loops[0][uvl].uv = (0, 0)
    pbf.loops[1][uvl].uv = (1, 0)
    pbf.loops[2][uvl].uv = (1, 1)
    pbf.loops[3][uvl].uv = (0, 1)

    # 5 red light discs
    for i in range(n_lights):
        frac = (i / (n_lights - 1)) - 0.5  # -0.5 to 0.5
        offset = frac * light_spread
        lcx = cx + nx_n * offset
        lcy = cy + ny_n * offset
        center = bm.verts.new((lcx, lcy, light_z))
        ring = []
        for s in range(light_seg):
            angle = 2.0 * math.pi * s / light_seg
            # Disc in normal-Z plane (facing driving direction)
            dr = light_r * math.cos(angle)
            dz = light_r * math.sin(angle)
            vx = lcx + nx_n * dr - tx * 0.02
            vy = lcy + ny_n * dr - ty * 0.02
            vz = light_z + dz
            ring.append(bm.verts.new((vx, vy, vz)))
        for s in range(light_seg):
            s_next = (s + 1) % light_seg
            f = bm.faces.new([center, ring[s], ring[s_next]])
            f.material_index = 2

    bm.normal_update()
    bm.to_mesh(me)
    bm.free()
    me.update()

    ob.data.materials.append(mat_struct)
    ob.data.materials.append(mat_sponsor)
    ob.data.materials.append(mat_light)

    print(f"  [OK] Start gantry: 2 pillars, beam, sponsor panel, {n_lights} lights")
    return ob


# ============================================================
# AC Empties
# ============================================================

def build_empties(cl, norms, dists, start_idx, start_direction=None):
    """AC_START, AC_PIT, AC_TIME empties.

    start_direction: arrow heading in degrees from the layout editor (math angle,
    0°=east, 90°=north). When provided, determines which side of the startline
    is "behind" (opposite to the arrow) and the heading of the grid empties.
    """
    n = len(cl)
    tl = dists[-1]
    step = tl / n if n else 1
    sp = max(1, int(8.0 / step))
    hw = ROAD_WIDTH / 2
    objs = []

    # Centerline tangent at start point
    si = start_idx % n
    sj = (si + 1) % n
    stx = cl[sj][0] - cl[si][0]
    sty = cl[sj][1] - cl[si][1]
    stl = max((stx**2 + sty**2)**0.5, 1e-6)
    stx /= stl; sty /= stl

    if start_direction is not None:
        # Use arrow direction for heading and offset sign
        dir_rad = math.radians(start_direction)
        arrow_x, arrow_y = math.cos(dir_rad), math.sin(dir_rad)
        start_heading = math.atan2(-arrow_x, arrow_y)
        # Dot product: if negative, arrow opposes centerline → flip offset
        dot = arrow_x * stx + arrow_y * sty
        sign = 1 if dot >= 0 else -1
    else:
        start_heading = math.atan2(-stx, sty)
        sign = 1

    def _place(name, i, side_off=0.0, heading_override=None):
        idx = i % n
        cx, cy = cl[idx]
        nx, ny = norms[idx]
        if heading_override is not None:
            h = heading_override
        else:
            j = (idx + sign) % n
            tx = cl[j][0] - cx
            ty = cl[j][1] - cy
            tl2 = max((tx**2 + ty**2)**0.5, 1e-6)
            tx /= tl2
            ty /= tl2
            h = math.atan2(-tx, ty)
        e = bpy.data.objects.new(name, None)
        e.empty_display_type = 'ARROWS'
        e.empty_display_size = 3.0 if name.startswith("AC_TIME") else 1.0
        e.location = (cx + nx * side_off, cy + ny * side_off, 0)
        e.rotation_euler = Euler((0, 0, h), 'XYZ')
        bpy.context.collection.objects.link(e)
        objs.append(e)

    # Grid: ~8m behind startline (opposite to arrow direction), all same heading
    grid_back = max(1, int(8.0 / step))
    for k in range(5):
        if k == 0:
            off = 0.0  # Pole position: centered on track
        else:
            side = 1 if k % 2 == 1 else -1
            off = side * 1.5
        _place(f"AC_START_{k}", start_idx - sign * (grid_back + k * sp), off,
               heading_override=start_heading)

    grass_off = -(hw + GRASS_WIDTH / 2)
    for k in range(5):
        _place(f"AC_PIT_{k}", start_idx - sign * k * sp, grass_off)

    _place("AC_TIME_0", start_idx)

    seg_d = []
    full_len = 0.0
    for i in range(n):
        j = (i + 1) % n
        d = ((cl[j][0] - cl[i][0])**2 + (cl[j][1] - cl[i][1])**2)**0.5
        seg_d.append(d)
        full_len += d
    half = full_len / 2.0
    cum = 0.0
    half_idx = start_idx
    for _ in range(n):
        cum += seg_d[half_idx % n]
        half_idx += sign
        if cum >= half:
            break
    _place("AC_TIME_1", half_idx % n)

    return objs


# ============================================================
# Viewport and reference
# ============================================================

def setup_viewport():
    """Set viewport to Material Preview so textures are visible on file open."""
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                for space in area.spaces:
                    if space.type == 'VIEW_3D':
                        space.shading.type = 'MATERIAL'
                        r3d = space.region_3d
                        if r3d:
                            r3d.view_rotation = (1, 0, 0, 0)
                            r3d.view_location = (0, 0, 0)
                            r3d.view_distance = 200


# ============================================================
# Main
# ============================================================

def main():
    print("=" * 60)
    print("Casaluce — One-shot Blend Initializer (GeoNodes)")
    print("=" * 60)

    if os.path.isfile(BLEND_PATH):
        print(f"\nWARNING: {BLEND_PATH} already exists!")
        print("This script is meant to run ONCE to create the initial .blend.")
        print("The existing file will be OVERWRITTEN.\n")

    print(f"Road width: {ROAD_WIDTH} m")

    print("\nClearing scene...")
    clear_scene()

    print("\nInterpolating centerline...")
    cl = interpolate_centerline(CONTROL_POINTS, pts_per_seg=20)
    nm = compute_normals(cl)
    ds = cum_distances(cl)
    hw = ROAD_WIDTH / 2
    print(f"  {len(cl)} points, length {ds[-1]:.0f} m")

    # Resample all curves at uniform 2m spacing for precise editing
    CURVE_SPACING = 2.0
    dense_ctrl = resample_at_distance(cl, spacing=CURVE_SPACING)
    dense_nm = compute_normals(dense_ctrl)
    dense_ds = cum_distances(dense_ctrl)
    print(f"  Resampled to {len(dense_ctrl)} vertices (every {CURVE_SPACING}m)")

    # Materials
    print("\nCreating materials...")
    m_asp = make_material("mat_asphalt", "asphalt.png", 0.45, 0.7, 0.1, 10)
    m_crb = make_material("mat_curb", "curb_rw.png", 0.5, 0.8, 0.15, 12)
    m_grs = make_material("mat_grass", "grass.png", 0.5, 0.6, 0.05, 5)
    m_bar = make_material("mat_barrier", "barrier.png", 0.5, 0.7, 0.2, 15)
    m_gnd = make_material("mat_ground", "grass.png", 0.4, 0.5, 0.05, 5)
    m_sln = make_material("mat_startline", "startline.png", 0.5, 0.8, 0.2, 15)
    m_sponsor = make_material("ac_sponsor", "sponsor1.png", ks_amb=0.6, ks_dif=0.8, ks_spec=0.1, ks_exp=5.0)
    m_light_red = make_color_material("ac_light_red", 0.8, 0.05, 0.05, ks_amb=0.8, ks_dif=0.3, ks_spec=0.5, ks_exp=20.0)

    # Road — live RoadGen GeoNodes (not applied)
    # Uses dense_ctrl (resampled at 2m) for precise polyline editing
    road_obj = build_road_with_roadgen(dense_ctrl, m_asp)

    # Curbs — from layout layers
    print("\nCreating CurbProfile + CurbGen GeoNodes...")
    curb_profile = create_curb_profile()
    curbgen_ng = create_curbgen_nodegroup()
    print("Building curbs from layout layers...")
    curbs = build_curbs_from_layers(_curb_layers, curbgen_ng, m_crb, curb_profile)
    print(f"  {len(curbs)} curb segments")

    # Grass — GeoNodes (path curves at 2m spacing)
    print("\nCreating GrassGen GeoNodes...")
    grassgen_ng = create_grassgen_nodegroup()
    print("Building grass (GeoNodes, live modifiers)...")
    grass = build_grass_geonodes(dense_ctrl, dense_nm, hw, grassgen_ng, m_grs)

    # Walls — bmesh (3 faces per section: outer, inner, top)
    print("\nBuilding walls from layout layers...")
    walls = build_walls_from_layers(_wall_layers, m_bar)

    # Ground
    print("\nBuilding ground...")
    build_ground(dense_ctrl, hw, m_gnd)

    # Startline
    print("\nBuilding startline...")
    if _start_data and _start_data.get("position"):
        sx, sy = _start_data["position"]
        sl_idx = find_nearest_index(
            [(x, y, 0) for x, y in dense_ctrl], sx, sy)
    else:
        # Default: index 0 (first point of the dense centerline)
        sl_idx = 0
    build_startline(dense_ctrl, dense_nm, sl_idx, m_sln)

    # Start gantry
    print("\nBuilding start gantry...")
    build_start_gantry(dense_ctrl, dense_nm, sl_idx, m_bar, m_sponsor, m_light_red)

    # Empties — use same sl_idx as startline so they're aligned
    print("\nBuilding AC empties...")
    arrow_dir = _start_data.get("direction") if _start_data else None
    em = build_empties(dense_ctrl, dense_nm, dense_ds, start_idx=sl_idx,
                       start_direction=arrow_dir)
    print(f"  {len(em)} empties")

    # Organize into collections for clear scene structure
    print("\nOrganizing into collections...")
    scene_coll = bpy.context.scene.collection
    default_coll = scene_coll.children[0] if scene_coll.children else scene_coll

    def make_coll(name):
        c = bpy.data.collections.new(name)
        scene_coll.children.link(c)
        return c

    coll_track = make_coll("Track")        # road, startline
    coll_curbs = make_coll("Curbs")        # curb meshes
    coll_grass = make_coll("Grass")        # grass meshes
    coll_walls = make_coll("Barriers")     # wall meshes
    coll_ground = make_coll("Ground")      # ground plane
    coll_ac = make_coll("AC Nodes")        # empties (AC_START, AC_PIT, AC_TIME)
    coll_additional = make_coll("AdditionalElements")  # decorative elements (gantry, etc.)
    coll_helpers = make_coll("Helpers")    # path/edge curves (hidden)

    def move_to(obj, target_coll):
        for c in obj.users_collection:
            c.objects.unlink(obj)
        target_coll.objects.link(obj)

    for obj in list(bpy.data.objects):
        name = obj.name
        # CurbProfile → hidden Helpers
        if name == "CurbProfile":
            move_to(obj, coll_helpers)
        elif name == "1ROAD" or name == "1STARTLINE":
            move_to(obj, coll_track)
        elif "KERB" in name:
            move_to(obj, coll_curbs)
        elif "GRASS" in name:
            move_to(obj, coll_grass)
        elif "WALL" in name:
            move_to(obj, coll_walls)
        elif "GROUND" in name:
            move_to(obj, coll_ground)
        elif name == "1GANTRY":
            move_to(obj, coll_additional)
        elif name.startswith("AC_"):
            move_to(obj, coll_ac)

    # Hide Helpers collection in viewport (still visible in outliner)
    coll_helpers.hide_viewport = True

    # Remove default collection if empty
    if default_coll != scene_coll and len(default_coll.objects) == 0:
        bpy.data.collections.remove(default_coll)

    print("  [OK] Collections: Track, Curbs, Grass, Barriers, Ground, AC Nodes, AdditionalElements, Helpers (hidden)")

    # Viewport setup
    print("\nSetting viewport to Material Preview...")
    setup_viewport()

    # Save
    print(f"\nSaving {BLEND_PATH}...")
    bpy.ops.wm.save_as_mainfile(filepath=BLEND_PATH)

    # Summary
    n_geonodes = 1 + len(curbs) + len(grass) + len(walls)  # road + curbs + grass + walls
    print(f"\nDone!")
    print(f"  Centerline: {ds[-1]:.0f}m")
    print(f"  GeoNodes objects: {n_geonodes} (road, {len(curbs)} curbs, {len(grass)} grass, {len(walls)} walls)")
    print(f"  Static meshes: 2 (ground, startline)")
    print(f"  Empties: {len(em)}")
    print(f"\nNode groups: RoadGen, CurbGen, GrassGen")
    print(f"\nNext steps:")
    print(f"  1. Open {_slug}.blend in Blender")
    print(f"  2. Edit road (select 1ROAD polyline, edit control points)")
    print(f"  3. Adjust GeoNodes parameters (Width, Height, etc.)")
    print(f"  4. Use ac-track-tools to validate and configure surfaces")
    print(f"  5. Run: bash build.sh  (or python build_cli.py)")


if __name__ == "__main__":
    main()

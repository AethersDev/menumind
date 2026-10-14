"""The Spike in 3D: every frame of the film, rendered by Blender on the Metal GPU (EEVEE; Cycles
with SPIKE3D_ENGINE=CYCLES).

    blender --background --python video/spike3d.py -- FILM PAPERS OUT WINDOWS [FRAME ...]

FILM is the film's own state frame by frame, exported by spike_render.py from the page's
timeline: the camera, how dark the pass is, the printer's light, where every piece of paper is
and how it is turned. So the score's cues land where they were composed. PAPERS holds each
piece of paper's texture, one per state of its stamps. WINDOWS is a JSON object with the two
stretches that have their own camera: "open" (pulling back from the spike's tip to the film's
first framing) and "later" (circling the tickets in the dark, three days on). With FRAME
numbers, only those frames are rendered; otherwise every frame not yet on disk.

Real-world scale: a film pixel is a quarter of a millimetre, so the spike is 18.6 cm tall and a
ticket 85 mm wide. The film's drawn graphics are laid over these frames afterwards; they line up
because the 3D camera reproduces the film's camera exactly for things on the plane of the spike.
"""
import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Euler, Vector

FPS = 30
K = 0.00025                       # metres per film pixel
ROD_X, FLOOR_Y = 1650, 1024       # film pixels: the rod, and the counter top (z = 0)
HOLE = 31                         # film pixels from a ticket's top edge to its hole
ROD_R, BASE_R, BASE_H = 0.0016, 0.03, 0.012
TIP = (FLOOR_Y - 280) * K         # the rod's tip, at film y 280
ROD_TOP = TIP - 0.014
LENS = 50                         # the film's camera: LENS / 36 * 1920 pixels per metre at 1 m
LAMP_W = 14.0                     # the heat lamp at full power
CURL = -1                         # which way paper on the spike curls (checked on a still)
SPIKE_TILT = 72                   # degrees from upright for paper on the spike: stiff receipts don't
                                  # lie quite flat, and their faces stay readable from the film's camera
SAMPLES = 96
ENGINE = __import__("os").environ.get("SPIKE3D_ENGINE", "EEVEE")   # EEVEE: ~1.5 s a frame here; CYCLES: ~9 s


def X(px: float) -> float:
    return (px - ROD_X) * K


def Z(py: float) -> float:
    return (FLOOR_Y - py) * K


def material(name, color, metallic=0.0, roughness=0.5):
    m = bpy.data.materials.new(name)
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*color, 1)
    b.inputs["Metallic"].default_value = metallic
    b.inputs["Roughness"].default_value = roughness
    return m


def glow(name, color, strength):
    m = material(name, (0, 0, 0))
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Emission Color"].default_value = (*color, 1)
    b.inputs["Emission Strength"].default_value = strength
    return m


def add(obj, mat):
    obj.data.materials.append(mat)
    for p in obj.data.polygons:
        p.use_smooth = True
    return obj


def box(name, x0, x1, y0, y1, z0, z1, mat, bevel=0.0):
    bpy.ops.mesh.primitive_cube_add(size=1, location=((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2))
    o = bpy.context.object
    o.name, o.scale = name, (abs(x1 - x0), abs(y1 - y0), abs(z1 - z0))
    bpy.ops.object.transform_apply(scale=True)
    o.data.materials.append(mat)
    if bevel:
        b = o.modifiers.new("bevel", "BEVEL")
        b.width, b.segments = bevel, 4
    return o


def brushed(name, color, roughness):
    """Steel brushed along X: a stretched noise varies the roughness."""
    m = material(name, color, 1.0, roughness)
    nt = m.node_tree
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 60.0
    mapping = nt.nodes.new("ShaderNodeMapping")
    mapping.inputs["Scale"].default_value = (1.0, 120.0, 1.0)
    coord = nt.nodes.new("ShaderNodeTexCoord")
    ramp = nt.nodes.new("ShaderNodeMapRange")
    ramp.inputs["To Min"].default_value = roughness * 0.7
    ramp.inputs["To Max"].default_value = roughness * 1.3
    nt.links.new(coord.outputs["Object"], mapping.inputs["Vector"])
    nt.links.new(mapping.outputs["Vector"], noise.inputs["Vector"])
    nt.links.new(noise.outputs["Fac"], ramp.inputs["Value"])
    nt.links.new(ramp.outputs["Result"], nt.nodes["Principled BSDF"].inputs["Roughness"])
    return m


def light(name, kind, loc, energy, color, size=0.1, target=(0, 0, 0.09)):
    data = bpy.data.lights.new(name, kind)
    data.energy, data.color = energy, color
    if kind == "AREA":
        data.size = size
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = loc
    if target is not None:  # area lights emit along -Z: aim them with TRACK_TO, never by hand
        aim = bpy.data.objects.new(name + "_aim", None)
        bpy.context.scene.collection.objects.link(aim)
        aim.location = target
        c = obj.constraints.new("TRACK_TO")
        c.target, c.track_axis, c.up_axis = aim, "TRACK_NEGATIVE_Z", "UP_Y"
    return obj


def paper(sheet: dict, folder: Path):
    """A piece of paper: an empty at its hole (or where its hole would be), and the sheet itself,
    whose texture mixes between its stamp states by the object's color (R: first stamp, G:
    second) and fades by its alpha."""
    w, h, pid = sheet["w"], sheet["h"], sheet["id"]
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=16, y_subdivisions=24, size=1)
    obj = bpy.context.object
    obj.name = pid
    for v in obj.data.vertices:  # grid in XY, -0.5..0.5: stand it in XZ facing the camera, hole at 0
        u, t = v.co.x + 0.5, 0.5 - v.co.y
        v.co = Vector(((u * w - w / 2) * K, 0.0, -(t * h - HOLE) * K))
    m = bpy.data.materials.new("paper-" + pid)
    nt = m.node_tree
    b = nt.nodes["Principled BSDF"]
    b.inputs["Roughness"].default_value = 0.55
    info = nt.nodes.new("ShaderNodeObjectInfo")
    split = nt.nodes.new("ShaderNodeSeparateColor")
    nt.links.new(info.outputs["Color"], split.inputs["Color"])
    color = alpha = None
    for i, path in enumerate(sorted(folder.glob(f"{pid}-*.png"))):
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = bpy.data.images.load(str(path))
        tex.extension = "CLIP"  # not REPEAT: a transparent edge would pick up the opposite edge
        if color is None:
            color, alpha = tex.outputs["Color"], tex.outputs["Alpha"]
            continue
        mix = nt.nodes.new("ShaderNodeMix")
        mix.data_type = "RGBA"
        nt.links.new(split.outputs["Red" if i == 1 else "Green"], mix.inputs["Factor"])
        nt.links.new(color, mix.inputs["A"])
        nt.links.new(tex.outputs["Color"], mix.inputs["B"])
        color = mix.outputs["Result"]
    if color is None:
        sys.exit(f"no texture for paper {pid} in {folder}")
    fade = nt.nodes.new("ShaderNodeMath")
    fade.operation = "MULTIPLY"
    nt.links.new(alpha, fade.inputs[0])
    nt.links.new(info.outputs["Alpha"], fade.inputs[1])
    nt.links.new(color, b.inputs["Base Color"])
    nt.links.new(fade.outputs["Value"], b.inputs["Alpha"])
    obj.data.materials.append(m)
    bend = obj.modifiers.new("curl", "SIMPLE_DEFORM")
    bend.deform_method, bend.deform_axis, bend.angle = "BEND", "X", 0.0
    anchor = bpy.data.objects.new(pid + "-hole", None)
    bpy.context.scene.collection.objects.link(anchor)
    obj.parent = anchor
    return anchor, obj


def build(first: dict, folder: Path):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    s = bpy.context.scene
    world = bpy.data.worlds.new("night")
    world.node_tree.nodes["Background"].inputs[0].default_value = (0.004, 0.005, 0.007, 1)
    s.world = world
    steel = material("steel", (0.92, 0.92, 0.94), 1.0, 0.12)
    dark_steel = material("dark steel", (0.16, 0.17, 0.19), 1.0, 0.32)

    # the pass: a steel counter, and the spike on it
    bpy.ops.mesh.primitive_plane_add(size=6, location=(0, 1.5, 0))
    add(bpy.context.object, brushed("pass", (0.20, 0.21, 0.23), 0.34))
    bpy.ops.mesh.primitive_cylinder_add(vertices=128, radius=BASE_R, depth=BASE_H, location=(0, 0, BASE_H / 2))
    base = add(bpy.context.object, material("base", (0.20, 0.21, 0.23), 1.0, 0.33))
    base.modifiers.new("bevel", "BEVEL").width = 0.0035
    rod = bpy.data.collections.new("rod")  # what the strip lights may reach
    bpy.ops.mesh.primitive_cylinder_add(vertices=48, radius=0.0042, depth=0.006, location=(0, 0, BASE_H + 0.003))
    collar = add(bpy.context.object, steel)
    collar.modifiers.new("bevel", "BEVEL").width = 0.0006
    rod.objects.link(collar)
    # no end caps on the rod and its point: a smoothed cap bends the normals of the edge it
    # shares with the side, which shows as a band at macro range; the ends are hidden anyway
    bpy.ops.mesh.primitive_cylinder_add(vertices=64, radius=ROD_R, depth=ROD_TOP - BASE_H,
                                        location=(0, 0, (ROD_TOP + BASE_H) / 2), end_fill_type="NOTHING")
    rod.objects.link(add(bpy.context.object, steel))
    bpy.ops.mesh.primitive_cone_add(vertices=64, radius1=ROD_R, radius2=0.00003, depth=TIP - ROD_TOP,
                                    location=(0, 0, (TIP + ROD_TOP) / 2), end_fill_type="NOTHING")
    rod.objects.link(add(bpy.context.object, steel))

    # the printer, hung from a gantry over the pass; paper leaves through its underside, so the
    # body hides a ticket until it is printed
    box("gantry", -1.0, 1.0, -0.045, 0.05, Z(-410), Z(-470), dark_steel, 0.002)
    box("printer", X(1070), X(1510), -0.035, 0.03, Z(152), Z(-410), material("printer", (0.015, 0.017, 0.02), 0.0, 0.32), 0.004)
    bpy.ops.object.text_add(location=(X(1110), -0.0362, Z(70)), rotation=(math.pi / 2, 0, 0))
    brand = bpy.context.object
    brand.data.body, brand.data.size, brand.data.extrude = "MenuMind", 0.012, 0.0003
    brand.data.materials.append(material("brand", (0.30, 0.34, 0.38), 0.0, 0.5))
    bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=0.0014, depth=0.001, location=(X(1440), -0.0356, Z(75)),
                                        rotation=(math.pi / 2, 0, 0))
    led = add(bpy.context.object, glow("led", (0.49, 0.94, 0.56), 0.0))
    bpy.ops.mesh.primitive_cylinder_add(vertices=32, radius=0.006, depth=X(1500) - X(1080),
                                        location=(X(1290), -0.07, Z(-400)), rotation=(0, math.pi / 2, 0))
    tube = add(bpy.context.object, glow("heat lamp tube", (1.0, 0.42, 0.13), 0.0))

    # the kitchen behind the pass, out of focus: warm and cool lights, as in the film
    for x, y, z, r, warm, power in [(-1.1, 2.2, 0.55, 0.035, 1, 18), (-0.45, 3.0, 0.9, 0.05, 1, 14),
                                    (0.35, 2.6, 0.35, 0.03, 0, 10), (0.9, 1.9, 0.7, 0.04, 1, 16),
                                    (1.6, 3.4, 0.5, 0.06, 0, 8), (-1.8, 3.2, 0.25, 0.045, 0, 7),
                                    (0.05, 3.8, 1.2, 0.07, 1, 10), (2.3, 2.8, 1.0, 0.05, 1, 12)]:
        bpy.ops.mesh.primitive_uv_sphere_add(radius=r, location=(x, y, z))
        add(bpy.context.object, glow("bokeh", (1.0, 0.62, 0.32) if warm else (0.55, 0.72, 1.0), power * 0.32))

    for x, y, energy, color in [(-0.30, 0.34, 20.0, (0.80, 0.88, 1.0)), (0.30, 0.40, 13.0, (1.0, 0.90, 0.80))]:
        strip = light("strip", "AREA", (x, y, 0.12), energy, color, target=(0, 0, 0.10))  # lines down the rod
        strip.data.shape, strip.data.size, strip.data.size_y = "RECTANGLE", 0.03, 0.5
        strip.visible_camera = False
        strip.light_linking.receiver_collection = rod  # the steel only: no streaks on the counter
    light("moon", "AREA", (-0.9, -0.2, 0.8), 3.0, (0.52, 0.66, 1.0), 0.8)
    light("fill", "AREA", (-0.5, -1.2, 0.45), 5.0, (0.78, 0.84, 1.0), 1.0, target=(X(1450), 0, 0.12))  # paper reads as paper
    # the heat lamp: warm, from above the pass and in front of it, onto the printer's slot and the spike
    lamp = light("heat lamp", "AREA", (X(1350), -0.28, 0.55), LAMP_W, (1.0, 0.55, 0.28), 0.35, target=(X(1450), 0, 0.06))

    papers = {p["id"]: paper(p, folder) for p in first["papers"]}

    cam_data = bpy.data.cameras.new("camera")
    cam_data.lens, cam_data.dof.use_dof, cam_data.dof.aperture_fstop = LENS, True, 2.8
    cam_data.dof.aperture_blades = 0  # round bokeh
    cam = bpy.data.objects.new("camera", cam_data)
    s.collection.objects.link(cam)
    s.camera = cam

    if ENGINE == "EEVEE":
        s.render.engine = "BLENDER_EEVEE"
        s.eevee.taa_render_samples, s.eevee.use_raytracing = 48, True
        s.eevee.use_shadows = True
        for m in bpy.data.materials:  # dithered, not blended: paper must write depth, or depth of field blurs it
            if m.name.startswith("paper-"):  # and its shadow follows its outline: a torn half or a fanned
                m.surface_render_method = "DITHERED"  # stack must not shade itself with a whole sheet
                m.use_transparent_shadow = True
        return finish(s, cam, lamp, tube, led, papers)
    s.render.engine = "CYCLES"
    prefs = bpy.context.preferences.addons["cycles"].preferences
    prefs.compute_device_type = "METAL"
    prefs.get_devices()
    for d in prefs.devices:
        d.use = d.type == "METAL"
    if not any(d.use for d in prefs.devices):
        sys.exit("no Metal GPU found for Cycles")
    s.cycles.device = "GPU"
    s.cycles.samples, s.cycles.adaptive_threshold = SAMPLES, 0.02
    s.cycles.use_denoising, s.cycles.denoising_use_gpu = True, True
    s.cycles.max_bounces, s.cycles.glossy_bounces = 8, 6
    s.render.use_persistent_data = True
    return finish(s, cam, lamp, tube, led, papers)


def finish(s, cam, lamp, tube, led, papers):
    s.render.resolution_x, s.render.resolution_y, s.render.fps = 1920, 1080, FPS
    s.view_settings.view_transform, s.view_settings.look = "AgX", "AgX - Medium High Contrast"
    s.render.image_settings.file_format, s.render.image_settings.quality = "JPEG", 95
    return cam, lamp, tube, led, papers


def ease(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * x * (x * (6 * x - 15) + 10)


def smooth(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def lerp(a, b, k):
    return a + (b - a) * k


def film_pose(f: dict) -> tuple:
    """The film's own camera: the world pans and scales on screen; here the camera moves instead,
    level, at the distance that gives the same scale on the plane of the spike."""
    x, y, scale = f["cam"]
    sx, sy = f["shake"]
    cx, cy = (960 - x - sx) / scale, (540 - y - sy) / scale
    d = LENS / 36 * 1920 * K / scale
    return Vector((X(cx), -d, Z(cy))), Euler((math.pi / 2, 0, 0)).to_quaternion(), LENS, d


def orbit_pose(d: float, a: float, z: float, p: float, side: float, lens: float) -> tuple:
    """A camera on a cylinder round the spike: distance d, azimuth a, height z, pitch p (degrees),
    shifted sideways to hold the spike off centre."""
    a, p = math.radians(a), math.radians(p)
    view = Vector((math.sin(a), math.cos(a), 0.0))
    left = Vector((-math.cos(a), math.sin(a), 0.0))
    eye = Vector((0.0, 0.0, z)) - d * view + side * left
    look = view * math.cos(p) + Vector((0, 0, math.sin(p)))
    return eye, look.to_track_quat("-Z", "Y"), lens, d / math.cos(p)


def camera(t: float, f: dict, windows: dict, first_film: tuple) -> tuple:
    """The pose for time t: our own camera in the two windows, the film's everywhere else."""
    own_end = windows["open"]
    if t < own_end:  # from the tip, pulling back to exactly the film's first framing
        k = ease(t / (own_end - 0.2))
        eye, _, _, d_end = first_film
        return orbit_pose(lerp(0.10, d_end, k), lerp(35, 0, k), lerp(0.150, eye.z, k), lerp(12, 0, k),
                          lerp(0.008, -eye.x, k), LENS)
    start, flicker = windows["later"]
    w = smooth((t - start) / 1.2) * (1 - smooth((t - flicker) / 0.9)) if start <= t else 0.0
    mine = film_pose(f)
    if w <= 0:
        return mine
    k = ease((t - start) / (flicker - start - 0.2))
    other = orbit_pose(0.50, lerp(-40, -6, k), 0.28, -20.5, 0.0, 40)
    return (mine[0].lerp(other[0], w), mine[1].slerp(other[1], w), lerp(mine[2], other[2], w),
            lerp(mine[3], other[3], w))


def place(papers: dict, sheets: list) -> None:
    for depth, s in enumerate(sheets):  # each sheet its own plane: coplanar paper (a torn ticket's
                                        # halves) fights for depth and renders black
        anchor, obj = papers[s["id"]]
        obj.hide_render = s["opacity"] < 0.01
        if obj.hide_render:
            continue
        (x0, y0), (x1, y1), _ = s["corners"]
        ax, ay = s["anchor"]
        if s["kind"] == "receipt":  # paper leaves the printer: the clipped part is still inside it
            ay -= s["clipBottom"]
        tilt = s["tilt"] / 90
        z = Z(ay)
        if s["k"] is not None:  # on the spike the paper lies flat, stacked near the base
            drop = min(1.0, max(0.0, (ay - 210) / (700 - 18 * s["k"] - 210)))
            z = lerp(z, lerp(Z(210), BASE_H + 0.0035 + s["k"] * 0.0034, drop), tilt)  # clear of the base
        anchor.location = (X(ax), -0.0004 * depth, z)
        anchor.rotation_euler = (-math.radians(s["tilt"] * SPIKE_TILT / 90), 0.0, math.radians(s["az"]))
        obj.rotation_euler = (0.0, math.atan2(y1 - y0, x1 - x0), 0.0)
        obj.modifiers["curl"].angle = CURL * math.radians(18) * tilt
        stamps = [v / 0.92 for v in s["stamps"]] + [0.0, 0.0]
        obj.color = (min(1.0, stamps[0]), min(1.0, stamps[1]), 0.0, s["opacity"])


def main():
    argv = sys.argv[sys.argv.index("--") + 1:]
    film = json.loads(Path(argv[0]).read_text())
    folder, out, windows = Path(argv[1]), Path(argv[2]), json.loads(argv[3])
    chosen = [int(a) for a in argv[4:]]
    out.mkdir(parents=True, exist_ok=True)
    cam, lamp, tube, led, papers = build(film[0], folder)
    first_film = film_pose(film[round(windows["open"] * FPS)])
    s = bpy.context.scene
    for i in chosen or range(len(film)):
        path = out / f"{i:04d}.jpg"
        if not chosen and path.exists():
            continue
        t, f = i / FPS, film[i]
        eye, rot, lens, focus = camera(t, f, windows, first_film)
        cam.location, cam.rotation_euler = eye, rot.to_euler()
        cam.data.lens, cam.data.dof.focus_distance = lens, focus
        power = max(0.03, 1 - f["dark"])
        lamp.data.energy = LAMP_W * power
        tube.data.materials[0].node_tree.nodes["Principled BSDF"].inputs["Emission Strength"].default_value = 8 * power
        led.data.materials[0].node_tree.nodes["Principled BSDF"].inputs["Emission Strength"].default_value = 8 if f["led"] else 0
        place(papers, f["papers"])
        s.render.filepath = str(path)
        bpy.ops.render.render(write_still=True)
        print(f"SPIKE3D {i + 1}/{len(film)}", flush=True)


main()

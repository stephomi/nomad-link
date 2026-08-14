# SPDX-License-Identifier: MIT
"""A stand-in for Toolbag's mset module, close enough to test scene.py.

Field names mirror what Toolbag's stock shaders expose, so the name matching in
scene.py is exercised rather than bypassed.
"""

SCENE = []
MATERIALS = []
TEXTURES = {}


def reset():
    del SCENE[:]
    del MATERIALS[:]
    del FRAMED[:]
    TEXTURES.clear()


class Mesh:
    """Toolbag 5.03 takes the geometry at construction: Mesh(triangles, vertices)."""

    def __init__(self, triangles, vertices, uvs=None, colors=None, normals=None,
                 polygons=None):
        self.triangles = list(triangles)
        self.vertices = list(vertices)
        self.uvs = list(uvs or [])
        self.colors = list(colors or [])
        self.normals = list(normals or [])
        self.polygons = list(polygons or [])


class SceneObject:
    def __init__(self, name="Object"):
        self.destroyed = False
        self._name = name
        self.visible = True
        self.parent = None
        SCENE.append(self)

    @property
    def name(self):
        # a destroyed Toolbag object leaves a handle that raises when touched
        if self.destroyed:
            raise RuntimeError("object has been destroyed")
        return self._name

    @name.setter
    def name(self, value):
        if self.destroyed:
            raise RuntimeError("object has been destroyed")
        self._name = value

    def getChildren(self):
        return [o for o in SCENE if o.parent is self]

    def destroy(self):
        self.destroyed = True
        if self in SCENE:
            SCENE.remove(self)


class TransformObject(SceneObject):
    def __init__(self, name="Object"):
        SceneObject.__init__(self, name)
        self.position = [0.0, 0.0, 0.0]
        self.rotation = [0.0, 0.0, 0.0]
        self.scale = [1.0, 1.0, 1.0]


class MeshObject(TransformObject):
    def __init__(self, name="Mesh"):
        TransformObject.__init__(self, name)
        self._mesh = None   # a fresh Toolbag MeshObject has no mesh to fill in
        self.mesh_reads = 0

    @property
    def mesh(self):
        # counted: reading a written mesh back is what the bridge must not do
        self.mesh_reads += 1
        return self._mesh

    @mesh.setter
    def mesh(self, value):
        self._mesh = value

    def addSubmesh(self, name, material=None, startIndex=0, indexCount=-1):
        sub = SubMeshObject(name, material, startIndex, indexCount)
        sub.parent = self
        return sub

    def getBounds(self):
        """World bounds, rotating by z then x then y in degrees, like Toolbag."""
        import math
        points = self.mesh.vertices
        if not points:
            return None
        rx, ry, rz = (math.radians(a) for a in self.rotation)
        low = [float("inf")] * 3
        high = [float("-inf")] * 3
        for i in range(0, len(points), 3):
            x, y, z = points[i], points[i + 1], points[i + 2]
            x, y = x * math.cos(rz) - y * math.sin(rz), x * math.sin(rz) + y * math.cos(rz)
            y, z = y * math.cos(rx) - z * math.sin(rx), y * math.sin(rx) + z * math.cos(rx)
            x, z = x * math.cos(ry) + z * math.sin(ry), -x * math.sin(ry) + z * math.cos(ry)
            for slot, value in enumerate((x, y, z)):
                low[slot] = min(low[slot], value + self.position[slot])
                high[slot] = max(high[slot], value + self.position[slot])
        return [low, high]


class SubMeshObject(SceneObject):
    def __init__(self, name="Submesh", material=None, startIndex=0, indexCount=-1):
        SceneObject.__init__(self, name)
        self.material = material
        self.startIndex = startIndex
        self.indexCount = indexCount


class LightObject(TransformObject):
    def __init__(self, name="Light"):
        TransformObject.__init__(self, name)
        self.lightType = "omni"
        self.color = [1.0, 1.0, 1.0]
        self.useTemperature = False
        self.temperature = 6500.0
        self.brightness = 1.0
        self.spotAngle = 45.0
        self.spotSharpness = 0.0
        self.width = 0.0
        self.normalize = False
        self.lightScaling = True
        self.physicalUnits = False
        self.castShadows = True


class CameraObject(TransformObject):
    def __init__(self, name="Camera"):
        TransformObject.__init__(self, name)
        self.fov = 50.0
        self.mode = "perspective"
        self.orthoScale = 1.0


class SkyBoxObject(SceneObject):
    def __init__(self, name="Sky"):
        SceneObject.__init__(self, name)
        self.rotation = 0.0
        self.brightness = 1.0
        self.blur = 0.0
        self.mode = "sky"
        self.image = ""

    def importImage(self, path):
        self.image = path


class Texture:
    """Toolbag hands back a texture object, not the path that was assigned."""

    def __init__(self, path):
        self.path = path
        self.sRGB = True   # Toolbag's own default for a freshly loaded image


def findTexture(path):
    return TEXTURES.get(path)


class MaterialSubroutine:
    def __init__(self, name, fields):
        self.name = name
        self._fields = dict(fields)

    def getFieldNames(self):
        return list(self._fields)

    def getField(self, name):
        return self._fields[name]

    def setField(self, name, value):
        if name not in self._fields:
            raise ValueError("no such field: %s" % name)
        if isinstance(value, str) and "map" in name.lower():
            value = TEXTURES.setdefault(value, Texture(value))
        self._fields[name] = value


class Material:
    def __init__(self, name=""):
        self.name = name or "Material"
        self.assigned = []
        # names and fields as Toolbag 5.032 really reports them (see probe.py)
        self.albedo = MaterialSubroutine("Albedo", {"Albedo Map": None, "Color": [1.0, 1.0, 1.0]})
        self.microsurface = MaterialSubroutine(
            "Roughness", {"Roughness Map": None, "Channel": 0, "Roughness": 0.5,
                          "Invert;roughness": False})
        self.reflectivity = MaterialSubroutine(
            "Metalness", {"Metalness Map": None, "Channel": 0, "Metalness": 0.0,
                          "Invert": False})
        self.surface = MaterialSubroutine(
            "Normals", {"Normal Map": None, "Flip X": False, "Flip Y": False})
        self.diffusion = MaterialSubroutine("Lambertian", {})
        # a default Toolbag material leaves these slots empty
        self.emission = None
        self.occlusion = None
        self.transparency = None
        self.displacement = None
        MATERIALS.append(self)

    def setSubroutine(self, slot, shader):
        if slot == "albedo" and shader == "Vertex Color":
            self.albedo = MaterialSubroutine(
                "Vertex Color", {"Albedo Map": None, "Color": [1.0, 1.0, 1.0],
                                 "Vertex Color": True, "sRGB Color": False,
                                 "Vertex Alpha": False})
            return
        if slot == "albedo" and shader == "Albedo":
            self.albedo = MaterialSubroutine("Albedo",
                                             {"Albedo Map": None, "Color": [1.0, 1.0, 1.0]})
            return
        # the slots a default material leaves empty, with the one shader this build takes
        if slot == "emission" and shader == "Emissive":
            self.emission = MaterialSubroutine(
                "Emissive", {"Emissive Map": None, "Color": [1.0, 1.0, 1.0], "Intensity": 1.0})
            return
        if slot == "occlusion" and shader == "Occlusion":
            self.occlusion = MaterialSubroutine(
                "Occlusion", {"Occlusion Map": None, "Channel": 0, "Occlusion": 1.0})
            return
        if slot == "transparency" and shader == "Dither":
            self.transparency = MaterialSubroutine(
                "Dither", {"Alpha Map": None, "Channel": 3, "Alpha": 1.0})
            return
        if slot == "transparency" and shader in ("Add", "Refraction", "Cutout"):
            fields = {"Alpha Map": None, "Channel": 3, "Alpha": 1.0}
            if shader == "Refraction":
                fields["IOR"] = 1.5
            self.transparency = MaterialSubroutine(shader, fields)
            return
        if slot == "diffusion" and shader == "Subsurface Scatter":
            self.diffusion = MaterialSubroutine(
                "Subsurface Scatter", {"Scatter Map": None, "Subdermis Color": [1.0, 0.5, 0.4],
                                       "Depth": 1.0})
            return
        if slot == "diffusion" and shader == "Lambertian":
            self.diffusion = MaterialSubroutine("Lambertian", {})
            return
        if slot == "displacement" and shader == "Height":
            self.displacement = MaterialSubroutine(
                "Height", {"Height Map": None, "Channel": 0, "Scale": 1.0})
            return
        raise ValueError("unknown shader %s for %s" % (shader, slot))

    def assign(self, obj, includeChildren=True):
        self.assigned.append(obj)


class _Callbacks:
    onPeriodicUpdate = None
    onFrameUpdate = None
    onShutdownPlugin = None
    onSceneLoaded = None


callbacks = _Callbacks()


def getAllObjects():
    return list(SCENE)


def getAllObjectsOfType(class_name):
    # Toolbag 5.032 wants the class itself; the documented string is taken too
    if isinstance(class_name, type):
        return [o for o in SCENE if isinstance(o, class_name)]
    return [o for o in SCENE if type(o).__name__ == class_name]


def getAllMaterials():
    return list(MATERIALS)


def findObject(name):
    return next((o for o in SCENE if o.name == name), None)


def getCamera():
    camera = next((o for o in SCENE if isinstance(o, CameraObject)), None)
    return camera or CameraObject("Main Camera")


def getToolbagVersion():
    return 5030


def getSceneUnitScale():
    return 1.0


FRAMED = []


def frameObject(obj):
    FRAMED.append(obj)


PLUGIN_PATH = ""


def getPluginPath():
    return PLUGIN_PATH


def shutdownPlugin():
    pass


def log(text):
    pass


# UI: built and clicked by tests, drawn by nobody
class _Control:
    def __init__(self, value=None):
        self.value = value
        self.text = value if isinstance(value, str) else ""
        self.label = self.text
        self.width = 0.0
        self.onClick = None
        self.onChange = None

    def setMonospaced(self, monospaced):
        pass


class UIWindow(_Control):
    def __init__(self, title=""):
        _Control.__init__(self, title)
        self.title = title
        self.elements = []

    def addElement(self, child):
        self.elements.append(child)

    def addReturn(self):
        pass

    def addSpace(self, width):
        pass

    def getElements(self):
        return list(self.elements)

    def close(self):
        pass


class UILabel(_Control):
    pass


class UIButton(_Control):
    pass


class UICheckBox(_Control):
    def __init__(self, label=""):
        _Control.__init__(self, False)
        self.label = label


class UITextField(_Control):
    def __init__(self, value=""):
        _Control.__init__(self, value)


class UITextFieldInt(_Control):
    def __init__(self, value=0):
        _Control.__init__(self, value)

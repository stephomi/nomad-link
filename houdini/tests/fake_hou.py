# SPDX-License-Identifier: MIT
"""Just enough `hou` to exercise nodes.py outside Houdini.

This is a test double, not an emulator: it records what the SOP code asks for
and hands back plausible values, so the array bookkeeping in cook_in /
send_geometry can be checked without a Houdini licence.
"""
import sys
import types

import numpy


class Error(Exception):
    pass


class OperationFailed(Error):
    pass


class _AttribType:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return "attribType.%s" % self.name

    def __hash__(self):
        return hash(self.name)

    def __eq__(self, other):
        return isinstance(other, _AttribType) and other.name == self.name


class attribType:
    Point = _AttribType("Point")
    Vertex = _AttribType("Vertex")
    Prim = _AttribType("Prim")
    Global = _AttribType("Global")


class Attrib:
    def __init__(self, kind, name, default, size):
        self._type = kind
        self._name = name
        self.default = default
        self._size = size

    def type(self):
        return self._type

    def size(self):
        return self._size

    def name(self):
        return self._name


class Geometry:
    def __init__(self):
        self.points = numpy.zeros((0, 3))
        self.polygons = []
        self.attribs = {}
        self.values = {}
        self.globals = {}

    # ---- writing (In SOP)
    def createPoints(self, positions):
        self.points = numpy.asarray(positions, "f8")
        return list(range(len(self.points)))

    def createPolygons(self, faces, is_closed=True):
        self.polygons = [list(face) for face in faces]
        return self.polygons

    def addAttrib(self, kind, name, default):
        size = len(default) if isinstance(default, (tuple, list)) else 1
        self.attribs[(kind, name)] = Attrib(kind, name, default, size)
        return self.attribs[(kind, name)]

    def _store(self, kind, name, values):
        self.values[(kind, name)] = numpy.asarray(values)

    def setPointFloatAttribValues(self, name, values):
        self._store(attribType.Point, name, values)

    def setVertexFloatAttribValues(self, name, values):
        self._store(attribType.Vertex, name, values)

    def setPrimFloatAttribValues(self, name, values):
        self._store(attribType.Prim, name, values)

    def setPrimIntAttribValues(self, name, values):
        self._store(attribType.Prim, name, values)

    def setPrimStringAttribValues(self, name, values):
        self.values[(attribType.Prim, name)] = list(values)

    def setGlobalAttribValue(self, name, value):
        self.globals[name] = value

    # ---- reading (Out SOP)
    def intrinsicValue(self, name):
        return len(self.points) if name == "pointcount" else 0

    def findPointAttrib(self, name):
        return self.attribs.get((attribType.Point, name))

    def findVertexAttrib(self, name):
        return self.attribs.get((attribType.Vertex, name))

    def findPrimAttrib(self, name):
        return self.attribs.get((attribType.Prim, name))

    def pointFloatAttribValues(self, name):
        if name == "P":
            return tuple(self.points.ravel())
        return tuple(numpy.asarray(self.values[(attribType.Point, name)]).ravel())

    def vertexFloatAttribValues(self, name):
        return tuple(numpy.asarray(self.values[(attribType.Vertex, name)]).ravel())

    def vertexIntAttribValues(self, name):
        return tuple(int(v) for v in numpy.asarray(self.values[(attribType.Vertex, name)]).ravel())

    def primIntAttribValues(self, name):
        return tuple(int(v) for v in numpy.asarray(self.values[(attribType.Prim, name)]).ravel())


class Parm:
    def __init__(self, node, name, value):
        self._node = node
        self._name = name
        self.value = value

    def eval(self):
        return self.value

    def evalAsString(self):
        return str(self.value)

    def set(self, value):
        self.value = value


class Node:
    def __init__(self, name="node", parms=None, parent=None, geometry=None):
        self._name = name
        self._parent = parent
        self._parms = {key: Parm(self, key, value) for key, value in (parms or {}).items()}
        self._geometry = geometry if geometry is not None else Geometry()
        self.children = {}

    def parm(self, name):
        return self._parms.get(name)

    def evalParm(self, name):
        parm = self.parm(name)
        if parm is None and self._parent is not None:
            return self._parent.evalParm(name)
        return parm.eval() if parm is not None else 0

    def parent(self):
        return self._parent

    def geometry(self):
        return self._geometry

    def node(self, name):
        return self.children.get(name)

    def name(self):
        return self._name

    def path(self):
        return ("%s/%s" % (self._parent.path(), self._name)) if self._parent else "/obj/%s" % self._name

    def creator(self):
        raise Error("no creator in the test double")


def install():
    """Put the fake in sys.modules so `import hou` picks it up."""
    module = types.ModuleType("hou")
    module.attribType = attribType
    module.Error = Error
    module.OperationFailed = OperationFailed
    module.Geometry = Geometry
    module.Node = Node
    module.isUIAvailable = lambda: False
    module.homeHoudiniDirectory = lambda: "/tmp"
    module.sopNodeTypeCategory = lambda: "Sop"
    module.nodeType = lambda category, name: None
    module.node = lambda path: None
    sys.modules["hou"] = module
    return module

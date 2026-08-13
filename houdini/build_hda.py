# SPDX-License-Identifier: MIT
"""Build otls/nomad_link.hda (Nomad Link In + Nomad Link Out SOPs).

Run once, from Houdini's Python Shell or from a terminal:

    hython build_hda.py

Both assets are thin wrappers around the `nomad_link` Python package, so this
only has to be re-run when the parameter interface or the internal network
changes -- not when the Python changes.
"""
import os
import sys

import hou

HERE = os.path.dirname(os.path.abspath(__file__))
HDA_FILE = os.path.join(HERE, "otls", "nomad_link.hda")

PY = hou.scriptLanguage.Python


def button(name, label, call, help_text=""):
    parm = hou.ButtonParmTemplate(name, label)
    parm.setScriptCallback("import nomad_link; nomad_link.%s(kwargs)" % call)
    parm.setScriptCallbackLanguage(PY)
    if help_text:
        parm.setHelp(help_text)
    return parm


def hidden(template):
    template.hide(True)
    return template


def toggle(name, label, default=True, help_text=""):
    parm = hou.ToggleParmTemplate(name, label, default_value=default)
    if help_text:
        parm.setHelp(help_text)
    return parm


def connection_folder():
    host = hou.StringParmTemplate("host", "Host", 1, default_value=("",))
    host.setHelp("Nomad's address. Leave empty to discover it (UDP broadcast + Bonjour).")
    port = hou.IntParmTemplate("port", "Port", 1, default_value=(48312,))
    status = hou.StringParmTemplate("status", "Status", 1, default_value=("Disconnected",))
    status.setConditional(hou.parmCondType.DisableWhen, "{ 1 == 1 }")  # read-only display
    return hou.FolderParmTemplate(
        "connection", "Connection",
        (host, port,
         button("connect", "Connect", "connect_button"),
         button("disconnect", "Disconnect", "disconnect_button"),
         status),
        folder_type=hou.folderType.Simple,
    )


def transform_parms(reverse_help):
    scale = hou.FloatParmTemplate("scale", "Scale", 1, default_value=(1.0,), min=0.001, max=100.0)
    scale.setHelp("Uniform scale applied to positions crossing the link.")
    return [
        toggle("applyxform", "Apply Object Transform", True),
        toggle("reverse", "Flip Winding", True, reverse_help),
        scale,
    ]


def in_parm_group():
    group = hou.ParmTemplateGroup()
    group.append(connection_folder())

    source = hou.StringParmTemplate(
        "source", "Mesh", 1, default_value=("__all__",),
        menu_items=("__all__",), menu_labels=("All meshes",),
        item_generator_script="import nomad_link\nreturn nomad_link.mesh_menu()",
        item_generator_script_language=PY,
    )
    source.setHelp("Which received mesh to build. Rebuilt automatically as Nomad sends updates.")

    group.append(hou.FolderParmTemplate(
        "import", "Import",
        [button("getsel", "Get Selection", "get_selection",
                "Ask Nomad for its current selection."),
         button("getscene", "Get Scene", "get_scene",
                "Ask Nomad for every object in the scene."),
         source]
        + transform_parms("Nomad (glTF) is counter-clockwise front-facing, Houdini is clockwise.")
        + [toggle("importuv", "Import UVs", True),
           toggle("importcolor", "Import Colour and Paint", True),
           toggle("importgroups", "Import Face Groups", True)],
        folder_type=hou.folderType.Simple,
    ))

    # bumped by the client whenever new data lands, which is what recooks the SOP
    group.append(hidden(hou.IntParmTemplate("revision", "Revision", 1, default_value=(0,))))
    return group


def out_parm_group():
    group = hou.ParmTemplateGroup()
    group.append(connection_folder())

    name = hou.StringParmTemplate("meshname", "Name", 1, default_value=("$OS",))
    # filled in by Nomad's mesh_ack, so the same Houdini node keeps its Nomad object
    mesh_id = hidden(hou.StringParmTemplate("meshid", "Mesh Id", 1, default_value=("",)))
    geo_id = hidden(hou.StringParmTemplate("geoid", "Geometry Id", 1, default_value=("",)))

    group.append(hou.FolderParmTemplate(
        "export", "Export",
        [name,
         button("send", "Send to Nomad", "send_button"),
         toggle("autosend", "Auto Send on Change", False,
                "Send once per cook. Leave off for heavy geometry."),
         toggle("answer", "Answer Nomad's Get", True,
                "Reply to request_selection / request_scene with this geometry.")]
        + transform_parms("Houdini is clockwise front-facing, Nomad (glTF) is counter-clockwise.")
        + [toggle("senduv", "Send UVs", True),
           toggle("sendcolor", "Send Colour and Paint", True),
           mesh_id, geo_id],
        folder_type=hou.folderType.Simple,
    ))
    return group


def wrangle(parent, name, class_index, snippet):
    node = parent.createNode("attribwrangle", name)
    node.parm("class").set(class_index)  # 0 detail, 1 primitive, 2 point, 3 vertex
    node.parm("snippet").set(snippet)
    return node


def build_in(container):
    subnet = container.createNode("subnet", "nomad_link_in")
    for child in subnet.children():
        child.destroy()
    build = subnet.createNode("python", "build")
    build.parm("python").set("import nomad_link\nnomad_link.cook_in(hou.pwd())\n")
    output = subnet.createNode("output", "output0")
    output.setInput(0, build)
    output.setDisplayFlag(True)
    output.setRenderFlag(True)
    subnet.layoutChildren()
    return subnet, build


def build_out(container):
    subnet = container.createNode("subnet", "nomad_link_out")
    for child in subnet.children():
        child.destroy()
    convert = subnet.createNode("convert", "to_polygons")  # also unpacks polygon soups
    indirect = subnet.indirectInputs()[0]
    convert.setInput(0, indirect)
    vertex_points = wrangle(subnet, "vertex_points", 3, "i@nomad_vtxpt = @ptnum;")
    vertex_points.setInput(0, convert)
    prim_sizes = wrangle(subnet, "prim_sizes", 1, "i@nomad_nvtx = primvertexcount(0, @primnum);")
    prim_sizes.setInput(0, vertex_points)
    out = subnet.createNode("null", "OUT")
    out.setInput(0, prim_sizes)
    send = subnet.createNode("python", "send")
    send.parm("python").set("import nomad_link\nnomad_link.cook_out(hou.pwd())\n")
    send.setInput(0, out)
    output = subnet.createNode("output", "output0")
    output.setInput(0, send)
    output.setDisplayFlag(True)
    output.setRenderFlag(True)
    subnet.layoutChildren()
    return subnet, send


def link_parms(node, names):
    """Spare parms on the inner Python SOP that reference the asset's own parms.

    Cook dependencies only follow parameters the cooking node evaluates, so the
    Python SOP needs its own copies to recook when the asset's parms change.
    """
    definition = node.parent().type().definition()
    templates = definition.parmTemplateGroup()
    for name in names:
        template = templates.find(name)
        if template is None:
            continue
        if isinstance(template, hou.StringParmTemplate):
            template = hou.StringParmTemplate(name, template.label(), 1)  # drop the menu
        node.addSpareParmTuple(template)
        expression = 'chs("../%s")' % name if isinstance(template, hou.StringParmTemplate) \
            else 'ch("../%s")' % name
        node.parm(name).setExpression(expression, language=hou.exprLanguage.Hscript)


def make_asset(subnet, inner, name, label, parm_group, linked, min_inputs, max_inputs):
    asset = subnet.createDigitalAsset(
        name=name,
        hda_file_name=HDA_FILE,
        description=label,
        min_num_inputs=min_inputs,
        max_num_inputs=max_inputs,
        ignore_external_references=True,
    )
    definition = asset.type().definition()
    definition.setParmTemplateGroup(parm_group)
    asset.allowEditingOfContents()
    link_parms(asset.node(inner), linked)
    definition.updateFromNode(asset)
    asset.matchCurrentDefinition()
    return asset


def main():
    if not os.path.isdir(os.path.dirname(HDA_FILE)):
        os.makedirs(os.path.dirname(HDA_FILE))
    container = hou.node("/obj").createNode("geo", "nomad_link_build")

    subnet, build = build_in(container)
    make_asset(subnet, "build", "nomad_link_in", "Nomad Link In", in_parm_group(),
               ("revision", "source", "applyxform", "reverse", "scale",
                "importuv", "importcolor", "importgroups"), 0, 0)

    subnet, send = build_out(container)
    make_asset(subnet, "send", "nomad_link_out", "Nomad Link Out", out_parm_group(),
               ("autosend", "applyxform", "reverse", "scale", "senduv", "sendcolor"), 1, 1)

    container.destroy()
    hou.hda.installFile(HDA_FILE)
    print("wrote %s" % HDA_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())

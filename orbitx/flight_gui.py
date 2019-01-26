# -*- coding: utf-8 -*-
"""
Defines FlightGui, a class that provides a main loop for flight.

Call FlightGui.draw() in the main loop to update positions in the GUI.
Call FlightGui.pop_commands() to collect user input.
"""

import logging
import signal
from pathlib import Path

import numpy as np

from . import common
from . import orbitx_pb2 as protos

log = logging.getLogger()

DEFAULT_CENTRE = 'Habitat'
DEFAULT_REFERENCE = 'Earth'
DEFAULT_TARGET = 'Moon'
G = 6.674e-11


class FlightGui:

    def __init__(self, physical_state_to_draw, texture_path=None):
        ctrl_c_handler = signal.getsignal(signal.SIGINT)
        # Note that this might actually start an HTTP server!
        import vpython
        self._vpython = vpython

        if self._vpython.__version__ == '7.4.7':
            # vpython installs "os._exit(0)" signal handlers in 7.4.7.
            # Very not good for us.
            # Reset the signal handler to default behaviour.
            signal.signal(signal.SIGINT, ctrl_c_handler)

            # os._exit(0) is in one other place, let's take that out too.
            # Unfortunately, now that vpython isn't calling os._exit, we have
            # to notify someone when the GUI closed.
            # Note to access vpython double underscore variables, we have to
            # bypass normal name mangling by using getattr.
            def callback(*_):
                getattr(self._vpython.no_notebook, '__interact_loop').stop()
                self.closed = True
            self.closed = False
            getattr(self._vpython.no_notebook,
                    '__factory').protocol.onClose = callback

        # Set up our vpython canvas and other internal variables
        self._scene = vpython.canvas(
            title='<b>OrbitX</b>',
            align='right',
            width=800,
            height=600,
            center=vpython.vector(0, 0, 0),
            up=vpython.vector(0, 0, 1),
            forward=vpython.vector(0.1, 0.1, -1),
            autoscale=True
        )

        self._commands = []

        self._show_label = True
        self._pause = False
        self.pause_label = vpython.label(
            text="Simulation Paused.", visible=False)

        self._scene.bind('keydown', self._handle_keydown)
        self._scene.bind('click', self._handle_click)

        self._spheres = {}
        self._sphere_parts = {}
        self._labels = {}

        self._texture_path = texture_path
        if texture_path is None:
            # If we're in src/ look for src/../textures/
            self._texture_path = Path('data', 'textures')
        # stars = str(self._texture_path / 'Stars.jpg')
        # vpython.sphere(radius=9999999999999, texture=stars)

        assert len(physical_state_to_draw.entities) >= 1
        self._last_physical_state = physical_state_to_draw
        self._origin = physical_state_to_draw.entities[0]
        self._reference = physical_state_to_draw.entities[0]
        self._target = physical_state_to_draw.entities[0]
        self._set_origin(DEFAULT_CENTRE)
        self._set_reference(DEFAULT_REFERENCE)
        self._set_target(DEFAULT_TARGET)
        self.draw_sphere_segments = False

        for planet in physical_state_to_draw.entities:
            self._spheres[planet.name] = self._draw_sphere(planet)
            self._sphere_parts[planet.name] = self._draw_sphere_segment(planet)
            if planet.name == 'Habitat':
                # Habitat isn't a sphere, and isn't big enough to need any LOD
                del self._sphere_parts[planet.name]
            self._labels[planet.name] = self._draw_labels(planet)
            if planet.name == DEFAULT_CENTRE:
                self.recentre_camera(DEFAULT_CENTRE)
        self._scene.autoscale = False
        self._set_caption()

    def shutdown(self):
        """Stops any threads vpython has started. Call on exit."""
        if not self._vpython._isnotebook and \
                self._vpython.__version__ == '7.4.7':
            # We're not running in a jupyter notebook environment. In
            # vpython 7.4.7, that means an HTTPServer and a WebSocketServer
            # are running, each in their own thread. From comments in
            # vpython.py at about line 370:
            # "The situation with non-notebook use is similar, but the http
            # server is threaded, in order to serve glowcomm.html, jpg texture
            # files, and font files, and the  websocket is also threaded."
            # This is fixed in the 2019 release of vpython, 7.5.0

            # Again, double underscore names will get name mangled unless we
            # bypass name mangling with getattr.
            getattr(self._vpython.no_notebook, '__server').shutdown()
            getattr(self._vpython.no_notebook, '__interact_loop').stop()

    def _calc_orb_speed(self, ref_entity):
        """The orbital speed of an astronomical body or object.

        Equation referenced from https://en.wikipedia.org/wiki/Orbital_speed"""
        return self._vpython.sqrt((G * ref_entity.mass) / ref_entity.r)

    def _altitude(self, planet_name, planet_name2='Habitat'):
        """Caculate distance between ref_planet and Habitat"""
        planet = common.find_entity(planet_name, self._last_physical_state)
        planet2 = common.find_entity(planet_name2, self._last_physical_state)
        return self._vpython.mag(self._vpython.vector(
            planet.x - planet2.x,
            planet.y - planet2.y,
            0
        )) - planet.r - planet2.r

    def _speed(self, planet_name, planet_name2='Habitat'):
        """Caculate distance between ref_planet and Habitat"""
        planet = common.find_entity(planet_name, self._last_physical_state)
        planet2 = common.find_entity(planet_name2, self._last_physical_state)
        return self._vpython.mag(self._vpython.vector(
            planet.vx - planet2.vx,
            planet.vy - planet2.vy,
            0
        ))

    def _posn(self, entity):
        """Translates into the frame of reference of the origin."""
        return self._vpython.vector(
            entity.x - self._origin.x,
            entity.y - self._origin.y,
            0)

    def _unit_velocity(self, entity):
        """Provides entity velocity relative to reference."""
        return self._vpython.vector(
            entity.vx - self._reference.vx,
            entity.vy - self._reference.vy,
            0).norm()

    def _set_reference(self, entity_name):
        try:
            self._reference = common.find_entity(
                entity_name, self._last_physical_state)
        except IndexError:
            log.error(f'Tried to set non-existent reference "{entity_name}"')

    def _set_target(self, entity_name):
        try:
            self._target = common.find_entity(
                entity_name, self._last_physical_state)
        except IndexError:
            log.error(f'Tried to set non-existent target "{entity_name}"')

    def _set_origin(self, entity_name):
        """Set origin position for rendering universe and reset the trails.

        Because GPU(Graphics Processing Unit) cannot deal with extreme case of
        scene center being approximately 1e11 (planets position), origin
        entity should be reset every time when making a scene.center update.
        """
        try:
            self._origin = common.find_entity(
                entity_name, self._last_physical_state)
        except IndexError:
            log.error(f'Tried to set non-existent origin "{entity_name}"')

    def _clear_trails(self):
        for sphere in self._spheres.values():
            sphere.clear_trail()

    def _show_hide_label(self):
        if self._show_label:
            for key, label in self._labels.items():
                label.visible = True
        else:
            for key, label in self._labels.items():
                label.visible = False

    def pop_commands(self):
        """Take gathered user input and send it off."""
        old_commands = self._commands
        self._commands = []
        return old_commands

    def _handle_keydown(self, evt):
        """Input key handler"""

        k = evt.key
        if k == 'l':
            self._show_label = not self._show_label
            self._show_hide_label()
        elif k == 'p':
            self._pause = not self._pause
        elif k == 'a':
            self._commands.append(protos.Command(
                ident=protos.Command.HAB_SPIN_CHANGE,
                arg=np.radians(10)))
        elif k == 'd':
            self._commands.append(protos.Command(
                ident=protos.Command.HAB_SPIN_CHANGE,
                arg=-np.radians(10)))
        elif k == 'w':
            self._commands.append(protos.Command(
                ident=protos.Command.HAB_THROTTLE_CHANGE,
                arg=0.01))
        elif k == 's':
            self._commands.append(protos.Command(
                ident=protos.Command.HAB_THROTTLE_CHANGE,
                arg=-0.01))

        # elif (k == 'e'):
        #    self._scene.center = self._spheres['Earth'].pos
        #
        # elif (k == 's'):
        #    self._scene.center = self._spheres['Sun'].pos
        #
        # elif (k == 't'):
        #    self.cur_caption = (self.cur_caption + 1) % 4
        #    self.set_caption()

    def _handle_click(self, evt):
        # global obj, clicked
        try:
            obj = self._scene.mouse.pick
            if obj is not None:
                self.update_caption(obj)

        except AttributeError:
            pass
        # clicked = True

    # TODO: 1)Update with correct physics values
    def _set_caption(self):
        """Set and update the captions."""

        # There's a bit of magic here. Normally, vpython.wtext will make a
        # <div> in the HTML and automaticall update it when the .text field is
        # updated in this python code. But if you want to insert a wtext in the
        # middle of a field, the following first attempt won't work:
        #     scene.append_to_caption('<table>')
        #     vpython.wtext(text='widget text')
        #     scene.append_to_caption('</table>')
        # because adding the wtext will also close the <table> tag.
        # But you can't make a wtext that contains HTML DOM tags either,
        # because every time the text changes several times a second, any open
        # dropdown menus will be closed.
        # So we have to insert a <div> where vpython expects it, manually.
        # We take advantage of the fact that manually modifying scene.caption
        # will remove the <div> that represents a wtext. Then we add the <div>
        # back, along with the id="x" that identifies the div, used by vpython.
        #
        # TL;DR the div_id variable is a bit magic, if you make a new wtext
        # before this, increment div_id by one..
        self._scene.caption += "<table>\n"
        self._wtexts = []
        div_id = 1
        for caption, text_gen_func, helptext, new_section in [
            ("Orbit speed",
             lambda: f"{self._calc_orb_speed(self._reference):,.7g} m/s",
             "Speed required for circular orbit at current altitude",
             False),
            ("Ref alt",
             lambda: f"{self._altitude(self._reference.name):,.7g} m",
             "Altitude of habitat above reference surface",
             True),
            ("Ref speed",
             lambda: f"{self._speed(self._reference.name):,.7g} m/s",
             "Speed of habitat above reference surface",
             False),
            ("Targ alt",
             lambda: f"{self._altitude(self._target.name):,.7g} m",
             "Altitude of habitat above reference surface",
             True),
            ("Targ speed",
             lambda: f"{self._speed(self._target.name):,.7g} m/s",
             "Altitude of habitat above reference surface",
             False),
            ("Throttle",
             lambda: f"{self._habitat.throttle:.1%}",
             "Percentage of habitat's maximum rated engines",
             True)
        ]:
            self._wtexts.append(self._vpython.wtext(text=text_gen_func()))
            self._wtexts[-1].text_func = text_gen_func
            self._scene.caption += f"""<tr>
                <td {"class='newsection'" if new_section else ""}>
                    {caption}
                </td>
                <td class="num{" newsection" if new_section else ""}">
                    <div id="{div_id}">{self._wtexts[-1].text}</div>
                </td>
                <td class="helptext{" newsection" if new_section else ""}">
                    {helptext}
                </td>
                </tr>\n"""
            div_id += 1
        self._scene.caption += "</table>"

        #self._scene.append_to_caption("Acc: XXX" + "\n")
        #self._scene.append_to_caption("Vcen: XXX" + "\n")
        #self._scene.append_to_caption("Vtan: XXX" + "\n")
        # self._scene.append_to_caption("\n")
        self._set_menus()
        self._scene.append_to_caption(HELP_CHECKBOX)
        self._scene.append_to_caption(" Help text")
        self._scene.append_to_caption(INPUT_CHEATSHEET)

        self._scene.append_to_caption(VPYTHON_CSS)
        self._scene.append_to_caption(VPYTHON_JS)

    # TODO: create bind functions for target, ref, and NAV MODE
    def _set_menus(self):
        """This creates dropped down menu which is used when set_caption."""

        def build_menu(*, choices=None, bind=None,
                       selected=None, caption=None, helptext=None):
            self._vpython.menu(
                choices=choices,
                pos=self._scene.caption_anchor,
                bind=bind,
                selected=selected)
            self._scene.append_to_caption(f"&nbsp;<b>{caption}</b>&nbsp;")
            self._scene.append_to_caption(
                f"<span class='helptext'>{helptext}</span>")
            self._scene.append_to_caption("\n")

        build_menu(
            choices=list(self._spheres),
            bind=self._recentre_dropdown_hook,
            selected=DEFAULT_CENTRE,
            caption="Centre",
            helptext="Focus of camera"
        )

        build_menu(
            choices=list(self._spheres),
            bind=self._reference_dropdown_hook,
            selected=DEFAULT_REFERENCE,
            caption="Reference",
            helptext=(
                "Take position, velocity relative to this.")
        )

        build_menu(
            choices=list(self._spheres),
            bind=self._target_dropdown_hook,
            selected=DEFAULT_TARGET,
            caption="Target",
            helptext="For use by NAV mode"
        )

        build_menu(
            choices=['deprt ref'],
            bind=self._navmode_dropdown_hook,
            selected='deprt ref',
            caption="NAV mode",
            helptext="Automatically points habitat"
        )

        build_menu(
            choices=[f'{n:,}×' for n in
                     [1, 5, 10, 50, 100, 1_000, 10_000, 100_000]],
            bind=self._time_acc_dropdown_hook,
            selected=1,
            caption="Warp",
            helptext="Speed of simulation"
        )

        self._scene.append_to_caption("\n")
        self._vpython.checkbox(
            bind=self._trail_checkbox_hook, checked=False, text='Trails')
        self._scene.append_to_caption(
            " <span class='helptext'>Graphically intensive</span>")

    def draw(self, physical_state_to_draw):
        self._last_physical_state = physical_state_to_draw
        # Have to reset origin, reference, and target with new positions
        self._set_origin(self._origin.name)
        self._set_reference(self._reference.name)
        self._set_target(self._target.name)
        if self._pause:
            self._scene.pause("Simulation is paused. \n Press 'p' to continue")
        for planet in physical_state_to_draw.entities:
            self._update_sphere(planet)
            self._update_sphere_segment(planet)
            if self._show_label:
                self._update_label(planet)

        for wtext in self._wtexts:
            # Update text of all text widgets.
            wtext.text = wtext.text_func()

    def _draw_labels(self, planet):
        label = self._vpython.label(
            visible=True, pos=self._posn(planet),
            xoffset=0, yoffset=10, height=16,
            border=4, font='sans')

        label.text_function = lambda entity: entity.name
        if planet.name == 'Habitat':
            label.text_function = lambda entity: (
                f'{entity.name}\n'
                f'Fuel: {abs(round(entity.fuel, 1))} kg\n'
                f'Heading: {round(np.degrees(entity.heading))}\xb0'
            )
        label.text = label.text_function(planet)

        return label

    def _draw_sphere(self, planet):
        texture = self._texture_path / (planet.name + '.jpg')

        if planet.name == "Habitat":
            # Todo: habitat object should be seperately defined from planets
            obj = self._vpython.cone(
                pos=self._posn(planet),
                axis=self._ang_pos(planet.heading),
                radius=planet.r / 2,
                make_trail=False,
                shininess=0.1
            )
            obj.length = planet.r
            obj.arrow = self._unit_velocity(planet)
            self._habitat = planet
            self._vpython.attach_arrow(obj, 'arrow', scale=planet.r * 1.5)
        else:
            obj = self._vpython.sphere(
                pos=self._posn(planet),
                axis=self._ang_pos(planet.heading),
                up=self._vpython.vector(0, 0, 1),
                radius=planet.r,
                make_trail=False,
                shininess=0.1
            )

        obj.name = planet.name  # For convenient accessing later

        if planet.name == 'Sun':  # The sun is special!
            obj.emissive = True  # The sun glows!
            self._scene.lights = []
            self._lights = [self._vpython.local_light(pos=obj.pos)]

        if texture.is_file():
            obj.texture = str(texture)
        else:
            log.debug(f'Could not find texture {texture}')

        return obj

    def _draw_sphere_segment(self, planet):
        if not self.draw_sphere_segments:
            # DEBUG
            return

        if planet.name == 'Habitat':
            return None
        tri_coords = _build_sphere_segment_vertices(
            planet.r)

        # Tris will be all the triangles in the sphere segment, but start with
        # a triangle at 0, 0, 0. This will be the centre of the compound object
        # so we can rotate the compound object around this centre.
        tris = [self._vpython.triangle(vs=[
            self._vpython.vertex(pos=self._vpython.vector(1, 0, 0)),
            self._vpython.vertex(pos=self._vpython.vector(0.5, 0.5, 0)),
            self._vpython.vertex(pos=self._vpython.vector(0, -0.5, 0))
        ])]

        for tri in tri_coords:
            vec_tri = [self._vpython.vertex(pos=self._vpython.vector(*coord))
                       for coord in tri]
            tris.append(self._vpython.triangle(vs=vec_tri))

        return self._vpython.compound(
            tris, pos=self._posn(planet))

    def _update_sphere(self, planet):
        sphere = self._spheres[planet.name]
        sphere.pos = self._posn(planet)
        sphere.axis = self._ang_pos(planet.heading)
        if planet.name == 'Habitat':
            sphere.length = planet.r
            sphere.arrow = self._unit_velocity(planet)
            self._habitat = planet

    def _update_sphere_segment(self, planet):
        if not self.draw_sphere_segments:
            # DEBUG
            return
        if planet.name == 'Habitat':
            return
        sphere_segment = self._sphere_parts[planet.name]
        sphere_segment.pos = self._posn(planet)

    def _update_label(self, planet):
        label = self._labels[planet.name]
        label.text = label.text_function(planet)
        label.pos = self._posn(planet)

    def _recentre_dropdown_hook(self, selection):
        self.recentre_camera(selection.selected)
        self._set_origin(selection.selected)
        self._clear_trails()

    def _reference_dropdown_hook(self, selection):
        self._set_reference(selection.selected)

    def _target_dropdown_hook(self, selection):
        self._set_target(selection.selected)

    def _navmode_dropdown_hook(self, selection):
        self._set_navmode(selection.selected)

    def _time_acc_dropdown_hook(self, selection):
        time_acc = int(selection.selected.replace(',', '').replace('×', ''))
        self._commands.append(protos.Command(
            ident=protos.Command.TIME_ACC_CHANGE,
            arg=time_acc))

    def _trail_checkbox_hook(self, selection):
        for sphere in self._spheres.values():
            sphere.make_trail = selection.checked
            if not selection.checked:
                sphere.clear_trail()

    def recentre_camera(self, planet_name):
        """Change camera to focus on different object

        Because GPU(Graphics Processing Unit) cannot deal with extreme case of
        scene center being approximately 1e11 (planets position), origin
        entity should be reset every time when making a scene.center update.
        """
        try:
            if planet_name == "Sun":
                self._scene.range = self._spheres["Sun"].radius * 15000
            else:
                self._scene.range = self._spheres[planet_name].radius * 10

            self._scene.camera.follow(self._spheres[planet_name])
            self._origin = common.find_entity(
                planet_name, self._last_physical_state)

        except KeyError:
            log.error(f'Unrecognized planet to follow: "{planet_name}"')
        except IndexError:
            log.error(f'Unrecognized planet to follow: "{planet_name}"')

    def _ang_pos(self, angle):
        return self._vpython.vector(np.cos(angle), np.sin(angle), 0)

    def rate(self, framerate):
        """Alias for vpython.rate(framerate). Basically sleeps 1/framerate"""
        self._vpython.rate(framerate)


VPYTHON_CSS = """<style>
table {
    margin-top: 1em;
    margin-bottom: 1em;
}
th {
    text-align: left;
}
.newsection {
    padding-top: 1em;
}
.num {
    font-family: monospace;
    font-weight: bold;
}
select {
    width: 100px;
}
input {
    width: 100px !important;
    height: unset !important;
}
.helptext {
    font-size: 75%;
}
</style>
<style title="disable_helptext">
.helptext {
    display: none;
}
</style>"""

VPYTHON_JS = """<script>
function show_hide_help() {
    // If helptext is enabled, we have to "disable disabling it."
    styleSheets = document.styleSheets;
    for (i = 0; i < styleSheets.length; ++i) {
        console.log(styleSheets[i].title)
        if (styleSheets[i].title == "disable_helptext") {
            styleSheets[i].disabled =
                document.getElementById("helptext_checkbox").checked
        }
    }
}

// Keep inputs deselected, so that keyboard input doesn't interfere with them.
for (var inp of document.querySelectorAll("input,select")) {
    inp.onchange = function(){this.blur()};
}
</script>"""

HELP_CHECKBOX = """
<input type="checkbox" id="helptext_checkbox" onClick="show_hide_help()">"""

# TODO: there's room for automating tables here.
INPUT_CHEATSHEET = """
<table class="helptext">
    <caption>Input Cheatsheet</caption>
    <tr>
        <th>Input</th>
        <th>Action</th>
    </tr>
    <tr>
        <td>Scroll, or ALT/OPTION-drag</td>
        <td>Zoom View</td>
    </tr>
    <tr>
        <td>Ctrl-Click</td>
        <td>Rotate View</td>
    </tr>
    <tr>
        <td>W/S</td>
        <td>Throttle Up/Down</td>
    </tr>
    <tr>
        <td>A/D</td>
        <td>Rotate Habitat</td>
    </tr>
    <tr>
        <td>L</td>
        <td>Toggle labels</td>
    </tr>
    <!-- re-enable this when we've fixed pausing
    <tr>
        <td>P</td>
        <td>Pause simulation</td>
    </tr>-->
</table>
"""


def _build_sphere_segment_vertices(
        radius, refine_steps=4, ang_size=np.deg2rad(30)):
    """Returns a segment of a sphere, which has a specified radius.
    The return is a list of xyz-tuples, each representing a vertex."""
    # This code inspired by:
    # http://blog.andreaskahler.com/2009/06/creating-icosphere-mesh-in-code.html
    # Thanks, Andreas Kahler.
    # TODO: limit the number of vertices generated to 65536 (the max in a
    # vpython compound object).

    # We set the 'middle' of the surface we're constructing to be (0, 0, r).
    # Think of this as a point on the surface of the sphere centred on (0,0,0)
    # with radius r.
    # Then, we construct four equilateral triangles that will all meet at
    # this point. Each triangle is ang_size degrees away from the 'middle'.

    # The values of 100 are placeholders, and get replaced by cos(ang_size) * r
    tris = np.array([
        ((0, 1, 100), (1, 0, 100), (0, 0, 100)),
        ((1, 0, 100), (0, -1, 100), (0, 0, 100)),
        ((0, -1, 100), (-1, 0, 100), (0, 0, 100)),
        ((-1, 0, 100), (0, 1, 100), (0, 0, 100))
    ])

    # Set the z of each xyz-tuple to be radius, and everything else to be
    # the coordinates on the radius-sphere times -1, 0, or 1.
    tris = np.where(
        [True, True, False],
        radius * np.sin(ang_size) * tris,
        radius * np.cos(ang_size))

    def middle_point(left, right):
        # Find the midpoint between the xyz-tuples left and right, but also
        # on the surface of a sphere (so not just a simple average).
        midpoint = (left + right) / 2
        midpoint_radial_dist = np.linalg.norm(midpoint)
        return radius * midpoint / midpoint_radial_dist

    for _ in range(0, refine_steps):
        new_tris = np.ndarray(shape=(0, 3, 3))
        for tri in tris:
            # A tri is a 3-tuple of xyz-tuples.
            a = middle_point(tri[0], tri[1])
            b = middle_point(tri[1], tri[2])
            c = middle_point(tri[2], tri[0])

            # Turn one triangle into a triforce projected onto a sphere.
            new_tris = np.append(new_tris, [
                (tri[0], a, c),
                (tri[1], b, a),
                (tri[2], c, b),
                (a, b, c)  # The centre triangle of the triforce
            ], axis=0)
        tris = new_tris

    return tris

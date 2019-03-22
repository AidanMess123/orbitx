# -*- coding: utf-8 -*-
"""
Defines FlightGui, a class that provides a main loop for flight.

Call FlightGui.draw() in the main loop to update positions in the GUI.
Call FlightGui.pop_commands() to collect user input.
"""

import logging
import signal
from pathlib import Path
from typing import Dict, List, Callable
import math


import numpy as np
from . import common
from . import orbitx_pb2 as protos  # physics module
import vpython                      # python 3D graphic library module

import orbitx.style as style        # HTML5, Javascript and CSS3 code for UI
import orbitx.calculator as calc
from orbitx.displayable import Displayable
from orbitx.planet import Planet
from orbitx.habitat import Habitat
from orbitx.spacestation import SpaceStation
from orbitx.star import Star
from orbitx.menu import Menu

log = logging.getLogger()

DEFAULT_CENTRE = 'Habitat'
DEFAULT_REFERENCE = 'Earth'
DEFAULT_TARGET = 'AYSE'

G = 6.674e-11

PLANET_SHININIESS = 0.3


class FlightGui:

    def __init__(
        self,
        physical_state_to_draw: protos.PhysicalState,
        texture_path: Path = None,
        no_intro: bool = False
    ) -> None:
        # Note that this might actually start an HTTP server!
        assert len(physical_state_to_draw.entities) >= 1
        ######################  attributes  ###########################
        self._last_physical_state = physical_state_to_draw
        self._minimap_canvas = self._init_minimap_canvas()
        # create a vpython canvas object
        self._scene: vpython.canvas = self._init_canvas()
        self._show_label: bool = True
        self._show_trails: bool = False
        self._pause: bool = False
        self.pause_label: vpython.label = None
        self._texture_path: Path = texture_path
        self._commands: list = []
        self._spheres: dict = {}
        self.pause_label: vpython.label = vpython.label(
            text="Simulation Paused.", visible=False)

        self._displaybles: Dict[str, Displayable] = {}
        # remove vpython ambient lighting
        self._scene.lights = []  # This line shouldn't be removed
        self._wtexts = []
        # self._menu: vpython.menu = Menu()
        ################################################################

        self._scene.autoscale: bool = False
        self._scene.range: float = 696000000.0 * 15000  # Sun radius * 15000

        # set texture path
        if texture_path is None:
            # Look for orbitx/data/textures
            self._texture_path = Path('data', 'textures')

        self._set_origin(DEFAULT_CENTRE)
        calc.ORT[0] = self._origin

        for planet in physical_state_to_draw.entities:
            obj: Displayable
            if planet.name == "Habitat":
                obj = Habitat(planet, self._texture_path,
                              self._scene, self._minimap_canvas)
            elif planet.name == "AYSE":
                obj = SpaceStation(planet, self._texture_path)
            elif planet.name == "Sun":
                obj = Star(planet, self._texture_path)
            else:
                obj = Planet(planet, self._texture_path)
            self._displaybles[planet.name] = obj
            self._spheres[planet.name] = obj

        self._set_reference(DEFAULT_REFERENCE)
        self._set_target(DEFAULT_TARGET)
        self._set_habitat("Habitat")
        calc.set_ORT(self._origin, self._reference, self._target,
                     self._habitat)

        self._set_caption()

        # Add an animation when launching the program
        #   to describe the solar system and the current location
        if not no_intro:
            while self._scene.range > 600000:
                vpython.rate(100)
                self._scene.range = self._scene.range * 0.92
        self.recentre_camera(DEFAULT_CENTRE)
    # end of __init__

    def _init_minimap_canvas(self) -> vpython.canvas:
        """Create a small sidebar scene showing the hab's orientation.
        This scene is filled in when the habitat is created."""
        # Make sure that the main canvas is still the default canvas.
        main_canvas = vpython.canvas.get_selected()
        miniamp_canvas = vpython.canvas(
            width=200, height=150, autoscale=True, userspin=False,
            up=vpython.vector(0.1, 0.1, 1), forward=vpython.vector(0, 0, -1))
        main_canvas.select()
        return miniamp_canvas
    # end of _init_minimap_canvas

    def _init_canvas(self) -> vpython.canvas:
        """Set up our vpython canvas and other internal variables"""

        _scene = vpython.canvas(
            title='<title>OrbitX</title>',
            align='right',
            width=800,
            height=800,
            center=vpython.vector(0, 0, 0),
            up=vpython.vector(0.1, 0.1, 1),
            forward=vpython.vector(0, 0, -1),
            autoscale=True
        )
        _scene.autoscale = False
        _scene.bind('keydown', self._handle_keydown)
        _scene.bind('click', self._handle_click)
        # Show all planets in solar system
        _scene.range = 696000000.0 * 15000  # Sun radius * 15000
        return _scene
    # end of _init_canvas

    def recentre_camera(self, planet_name: str) -> None:
        """Change camera to focus on different object

        Because GPU(Graphics Processing Unit) cannot deal with extreme case of
        scene center being approximately 1e11 (planets position), origin
        entity should be reset every time when making a scene.center update.
        """
        try:
            self._scene.range = self._displaybles[planet_name].relevant_range(
            )

            self._scene.camera.follow(self._displaybles[planet_name].get_obj())
            self._set_origin(planet_name)

        except KeyError:
            log.error(f'Unrecognized planet to follow: "{planet_name}"')
        except IndexError:
            log.error(f'Unrecognized planet to follow: "{planet_name}"')
    # end of recentre_camera

    def shutdown(self):
        """Stops any threads vpython has started. Call on exit."""
        if not vpython._isnotebook and \
                vpython.__version__ == '7.4.7':
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
            getattr(vpython.no_notebook, '__server').shutdown()
            getattr(vpython.no_notebook, '__interact_loop').stop()
    # end of shutdown

    def _find_entity(self, name: str) -> protos.Entity:
        return common.find_entity(name, self._last_physical_state)
    # end of _find_entity

    def _set_reference(self, entity_name: str) -> None:
        try:
            self._reference = self._find_entity(entity_name)
            self._displaybles[entity_name].draw_landing_graphic(
                self._reference)
        except IndexError:
            log.error(f'Tried to set non-existent reference "{entity_name}"')

    def _set_target(self, entity_name: str) -> None:
        try:
            self._target = self._find_entity(entity_name)
            self._displaybles[entity_name].draw_landing_graphic(
                self._target)
        except IndexError:
            log.error(f'Tried to set non-existent target "{entity_name}"')

    def _set_origin(self, entity_name: str) -> None:
        """Set origin position for rendering universe and reset the trails.

        Because GPU(Graphics Processing Unit) cannot deal with extreme case of
        scene center being approximately 1e11 (planets position), origin
        entity should be reset every time when making a scene.center update.
        """
        if self._show_trails:
            # The user is expecting to see trails relative to the reference.
            # We don't usually have this behaviour because if the reference is
            # far enough from the camera centre, we get graphical glitches.
            entity_name = self._reference.name

        try:
            self._origin = self._find_entity(entity_name)
        except IndexError:
            log.error(f'Tried to set non-existent origin "{entity_name}"')

    def _set_habitat(self, entity_name: str) -> None:
        try:
            self._habitat = self._find_entity(entity_name)
        except IndexError:
            log.error(f'Tried to set non-existent "{entity_name}"')

    def _clear_trails(self) -> None:
        for name, obj in self._displaybles.items():
            obj.clear_trail()
    # end of _clear_trails

    def _show_hide_label(self) -> None:
        for name, obj in self._displaybles.items():
            obj._show_hide_label()
    # end of _show_hide_label

    def pop_commands(self) -> list:
        """Take gathered user input and send it off."""
        old_commands = self._commands
        self._commands = []
        return old_commands

    def _handle_keydown(self, evt: vpython.event_return) -> None:
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
                spin_change=np.radians(10)))
        elif k == 'd':
            self._commands.append(protos.Command(
                ident=protos.Command.HAB_SPIN_CHANGE,
                spin_change=-np.radians(10)))
        elif k == 'w':
            self._commands.append(protos.Command(
                ident=protos.Command.HAB_THROTTLE_CHANGE,
                throttle_change=0.01))
        elif k == 's':
            self._commands.append(protos.Command(
                ident=protos.Command.HAB_THROTTLE_CHANGE,
                throttle_change=-0.01))
        elif k == 'W':
            self._commands.append(protos.Command(
                ident=protos.Command.HAB_THROTTLE_CHANGE,
                throttle_change=0.001))
        elif k == 'S':
            self._commands.append(protos.Command(
                ident=protos.Command.HAB_THROTTLE_CHANGE,
                throttle_change=-0.001))
        elif k == '\n':
            self._commands.append(protos.Command(
                ident=protos.Command.HAB_THROTTLE_SET,
                throttle_set=1.00))
        elif k == 'backspace':
            self._commands.append(protos.Command(
                ident=protos.Command.HAB_THROTTLE_SET,
                throttle_set=0.00))
    # end of _handle_keydown

    def _handle_click(self, evt: vpython.event_return) -> None:
        # global obj, clicked
        try:
            obj = self._scene.mouse.pick
            if obj is not None:
                self.update_caption(obj)

        except AttributeError:
            pass
        # clicked = True

    # TODO: 1)Update with correct physics values

    # TODO: create bind functions for target, ref, and NAV MODE

    def get_cpation_anchor(self) -> vpython.canvas:
        return self._scene.caption_anchor
    # end of get_cpation_anchor

    def get_spheres(self) -> List[protos.Entity]:
        return self._spheres
    # end of get_spheres

    def get_reference(self) -> protos.Entity:
        return self._reference
    # end of get_reference

    def get_target(self) -> protos.Entity:
        return self._target
    # end of get_target

    def get_origin(self) -> protos.Entity:
        return self._origin
    # end of get_origin

    def get_habitat(self) -> protos.Entity:
        return self._habitat
    # end of get_habitat

    def append_caption(self, caption: str) -> None:
        self._scene.append_to_caption(caption)
    # end of append_caption

    def concat_caption(self, caption: str) -> None:
        self._scene.caption += caption
    # end of concat_caption

    def append_wtexts(self, wtext: vpython.wtext) -> None:
        self._wtexts.append(wtext)
    # end of set_wtexts

    def set_wtexts_text_func_at(self, index: int, text_func) -> None:
        self._wtexts[index].text_func = text_func

    def wtexts_at(self, index: int) -> vpython.wtext:
        return self._wtexts[index]

    def set_centre_menu(self, menu: vpython.menu) -> None:
        self._centre_menu = menu
    # end of _set_centre_menu

    def set_time_acc_menu(self, menu: vpython.menu) -> None:
        self._time_acc_menu = menu
    # end of set_time_acc_menu

    def draw(self, physical_state_to_draw: protos.PhysicalState) -> None:
        self._last_physical_state = physical_state_to_draw
        # Have to reset origin, reference, and target with new positions
        self._habitat = self._find_entity("Habitat")
        self._origin = self._find_entity(self._origin.name)
        self._reference = self._find_entity(self._reference.name)
        self._target = self._find_entity(self._target.name)
        calc.set_ORT(self._origin, self._reference, self._target,
                     self._habitat)
        if self._pause:
            self._scene.pause("Simulation is paused. \n Press 'p' to continue")

        for planet in physical_state_to_draw.entities:
            self._displaybles[planet.name].draw(planet)
        # for

        for wtext in self._wtexts:
            # Update text of all text widgets.
            wtext.text = wtext.text_func()

    def _recentre_dropdown_hook(self, selection: vpython.menu) -> None:
        self._set_origin(selection.selected)
        self.recentre_camera(selection.selected)
        self._clear_trails()

    def _time_acc_dropdown_hook(self, selection: vpython.menu) -> None:
        time_acc = int(selection.selected.replace(',', '').replace('×', ''))
        self._commands.append(protos.Command(
            ident=protos.Command.TIME_ACC_SET,
            time_acc_set=time_acc))

    def _trail_checkbox_hook(self, selection: vpython.menu) -> None:
        self._show_trails = selection.checked
        for name, obj in self._displaybles.items():
            obj.trail_option(selection.checked)

        if not self._show_trails:
            # Turning on trails set our camera origin to be the reference,
            # instead of the camera centre. Revert that when we turn off trails
            self._set_origin(self._centre_menu.selected)

    def notify_time_acc_change(self, new_acc: int) -> None:
        new_acc_str = f'{new_acc:,}×'
        if new_acc_str == self._time_acc_menu.selected:
            return
        if new_acc_str not in self._time_acc_menu._choices:
            log.error(f'"{new_acc_str}" not a valid time acceleration')
            return
        self._time_acc_menu.selected = new_acc_str

    def rate(self, framerate: int) -> None:
        """Alias for vpython.rate(framerate). Basically sleeps 1/framerate"""
        vpython.rate(framerate)
    # end of rate

    # TODO: 1)Update with correct physics values
    def _set_caption(self) -> None:
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
             lambda: common.format_num(
                 calc.orb_speed(self._reference)) + " m/s",
             "Speed required for circular orbit at current altitude",
             False),
            ("Periapsis",
             lambda: common.format_num(
                 calc.periapsis(self._reference, self._habitat)) + " m",
             "Lowest altitude in naïve orbit around reference",
             False),
            ("Apoapsis",
             lambda: common.format_num(
                 calc.apoapsis(self._reference, self._habitat)) + " m",
             "Highest altitude in naïve orbit around reference",
             False),
            ("HRT phase θ",
             lambda: '{:.0f}'.format(calc.phase_angle()) + "°",
             "Angle between Habitat, Reference, and Target",
             False),
            ("Throttle",
             lambda: "{:.1%}".format(self._habitat.throttle),
             "Percentage of habitat's maximum rated engines",
             True),
            ("Fuel ",
             lambda: common.format_num(self._habitat.fuel) + " kg",
             "Remaining fuel of habitat",
             False),
            ("Ref altitude",
             lambda: common.format_num(
                calc.altitude(self._reference, self._habitat)) + " m",
             "Altitude of habitat above reference surface",
             True),
            ("Ref speed",
             lambda: common.format_num(
                calc.speed(self._reference, self._habitat)) + " m/s",
             "Speed of habitat above reference surface",
             False),
            ("Vertical speed",
             lambda: common.format_num(
                calc.v_speed(self._reference, self._habitat)) + " m/s ",
             "Vertical speed of habitat towards/away reference surface",
             False),
            ("Horizontal speed",
             lambda: common.format_num(
                calc.h_speed(self._reference, self._habitat)) + " m/s ",
             "Horizontal speed of habitat across reference surface",
             False),
            ("Targ altitude",
             lambda: common.format_num(
                calc.altitude(self._target, self._habitat)) + " m",
             "Altitude of habitat above reference surface",
             True),
            ("Targ speed",
             lambda: common.format_num(
                calc.speed(self._target, self._habitat)) + " m/s",
             "Speed of habitat above target surface",
             False)
            # TODO add pitch and stopping acceleration fields after symposium
        ]:
            self._wtexts.append(vpython.wtext(text=text_gen_func()))
            self._wtexts[-1].text_func = text_gen_func
            self._scene.caption += f"""<tr {"class='newsection'" if new_section else ""}>
            <td>
                {caption}
            </td >
            <td class="num">
                <div id = "{div_id}" >
                    {self._wtexts[-1].text}
                </div >
            <div class="helptext"
                style="font-size: 12px">
                    {helptext}
            </div >
            </td >
            </tr >\n"""
            div_id += 1
        self._scene.caption += "</table>"

        self._set_menus()
        self._scene.append_to_caption(style.HELP_CHECKBOX)
        self._scene.append_to_caption(" Help text")
        self._scene.append_to_caption("\t\t")
        vpython.button(
            text=" Switch ", pos=self._scene.caption_anchor,
            disabled=True, bind=self._switch)
        self._scene.append_to_caption(
            f"<span class='helptext'>Switch constrol to AYSE/Habitat</span>")

        self._scene.append_to_caption(style.INPUT_CHEATSHEET)

        self._scene.append_to_caption(style.VPYTHON_CSS)
        self._scene.append_to_caption(style.VPYTHON_JS)
    # end of _set_caption

    # TODO: create bind functions for target, ref, and NAV MODE
    def _set_menus(self) -> None:
        """This creates dropped down menu which is used when set_caption."""

        def build_menu(
            *,
            choices: list = None,
            bind=None,
            selected: str = None,
            caption: str = None,
            helptext: str = None
        ) -> vpython.menu:

            menu = vpython.menu(
                choices=choices,
                pos=self._scene.caption_anchor,
                bind=bind,
                selected=selected)
            self._scene.append_to_caption(f"&nbsp;<b>{caption}</b>&nbsp;")
            self._scene.append_to_caption(
                f"<span class='helptext'>{helptext}</span>")
            self._scene.append_to_caption("\n")
            return menu
        # end of build_menu

        self._centre_menu = build_menu(
            choices=list(self._spheres),
            bind=self._recentre_dropdown_hook,
            selected=DEFAULT_CENTRE,
            caption="Centre",
            helptext="Focus of camera"
        )

        build_menu(
            choices=list(self._spheres),
            bind=lambda selection: self._set_reference(selection.selected),
            selected=DEFAULT_REFERENCE,
            caption="Reference",
            helptext=(
                "Take position, velocity relative to this.")
        )

        build_menu(
            choices=list(self._spheres),
            bind=lambda selection: self._set_target(selection.selected),
            selected=DEFAULT_TARGET,
            caption="Target",
            helptext="For use by NAV mode"
        )

        build_menu(
            choices=['deprt ref'],
            bind=lambda selection: log.error(f"Unimplemented: {selection}"),
            selected='deprt ref',
            caption="NAV mode",
            helptext="Automatically points habitat"
        )

        self._time_acc_menu = build_menu(
            choices=[f'{n:,}×' for n in
                     [1, 5, 10, 50, 100, 1_000, 10_000, 100_000]],
            bind=self._time_acc_dropdown_hook,
            selected=1,
            caption="Warp",
            helptext="Speed of simulation"
        )

        self._scene.append_to_caption("\n")
        vpython.checkbox(
            bind=self._trail_checkbox_hook, checked=False, text='Trails')
        self._scene.append_to_caption(
            " <span class='helptext'>&nbspGraphically intensive</span>")

        self._scene.append_to_caption("\t\t\t")
        vpython.button(
            text="Undock", pos=self._scene.caption_anchor, bind=self._undock)
        self._scene.append_to_caption(
            f"<span class='helptext'>Dock to AYSE</span>")
    # end of _set_menus

    def _undock(self):
        print("undock")

    def _switch(self):
        print("switch")
# end of class FlightGui

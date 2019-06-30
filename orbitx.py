#!/usr/bin/env python3

"""
Main for CnC, the 'Command and Control' server

This is the central server that stores all state, serves it to networked
modules, and receives updates from networked modules.
"""

import argparse
import atexit
import logging
import time
import warnings
from pathlib import Path

import vpython

from orbitx import common
from orbitx.graphics import launcher
from orbitx.programs.lead import Lead
from orbitx.programs.mirror import Mirror
from orbitx.programs.compat import Compat

log = logging.getLogger()


def log_git_info():
    """For ease in debugging, try to get some version information.
    This should never throw a fatal error, it's just nice-to-know stuff."""
    try:
        git_dir = Path('.git')
        head_file = git_dir / 'HEAD'
        with head_file.open() as f:
            head_contents = f.readline().strip()
            log.info(f'Contents of .git/HEAD: {head_contents}')
        if head_contents.split()[0] == 'ref:':
            hash_file = git_dir / head_contents.split()[1]
            with hash_file.open() as f:
                log.info(f'Current reference hash: {f.readline().strip()}')
    except FileNotFoundError:
        return


def vpython_error_message():
    """Lets the user know that something bad happened.
    Note, if there's no vpython canvas, this vpython code has no effect."""
    error_message = (
        "<p>&#9888; Sorry, spacesimmer! OrbitX has crashed for "
        "some reason.</p>"

        "<p>Any information that OrbitX has on the crash has "
        "been saved to <span class='code'>orbitx/debug.log</span>. If you "
        "want to get this problem fixed, send the contents of the log file "
        "<blockquote>" +
        # Double-escape backslashes, vpython will choke on them otherwise.
        common.logfile_handler.baseFilename.replace('\\', '\\\\') +
        "</blockquote>"
        "Patrick Melanson along with a description of what was happening in "
        "the program when it crashed.</p>"

        "<p>Again, thank you for using OrbitX!</p>"
    )
    vpython.canvas.get_selected().append_to_caption(f"""<script>
        if (document.querySelector('div.error') == null) {{
            error_div = document.createElement('div');
            error_div.className = 'error';
            error_div.innerHTML = "{error_message}";
            document.querySelector('body').prepend(error_div);
        }}
    </script>""")
    vpython.canvas.get_selected().append_to_caption("""<style>
        .error {
            color: #D8000C;
            background-color: #FFBABA;
            margin: 10px 0;
            padding: 10px;
            border-radius: 3px 3px 3px 3px;
            width: 550px;
        }
        blockquote {
            font-family: monospace;
        }
    </style>""")

    time.sleep(0.1)  # Let vpython send out this update


def main():
    """Delegate work to one of the OrbitX programs."""

    # vpython uses deprecated python features, but we shouldn't get a fatal
    # exception if we try to use vpython. DeprecationWarnings are normally
    # enabled when __name__ == __main__
    warnings.filterwarnings('once', category=DeprecationWarning)
    # vpython generates other warnings, as well as its use of asyncio
    warnings.filterwarnings('ignore', category=ResourceWarning)
    warnings.filterwarnings('ignore', module='vpython|asyncio|autobahn')

    log_git_info()

    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--verbose', action='store_true', default=False,
                        help='Logs everything to both logfile and output.')

    parser.add_argument('--profile', action='store_true', default=False,
                        help='Generating profiling reports, for a flamegraph.')

    # Use the argument parsers that each program defines
    subparsers = parser.add_subparsers(help='Which OrbitX program to run',
                                       dest='program')

    for program in [Lead, Mirror, Compat]:
        subparser = subparsers.add_parser(
            program.argparser.prog,
            add_help=False, parents=[program.argparser])
        subparser.set_defaults(main_loop=program.main)

    args = parser.parse_args()

    if args.verbose:
        common.enable_verbose_logging()

    try:
        if args.program is None:
            # A program was specified, ask the user which program to run.
            program = launcher.Launcher().get_user_selection()

            # We got the user's choice, emulate the corresponding subparser
            # being run.
            args = parser.parse_args([program.argparser.prog])
            program.main(args)
        else:
            args.main_loop(args)
    except KeyboardInterrupt:
        # We're expecting ctrl-C will end the program, hide the exception from
        # the user.
        pass
    except Exception as e:
        log.exception('Unexpected exception, exiting...')
        atexit.unregister(common.print_handler_cleanup)

        vpython_error_message()

        if isinstance(e, (AttributeError, ValueError)):
            proto_file = Path('orbitx', 'orbitx.proto')
            generated_file = Path('orbitx', 'orbitx_pb2.py')
            if not generated_file.is_file():
                log.warning('================================================')
                log.warning(f'{proto_file} does not exist.')
            elif proto_file.stat().st_mtime > generated_file.stat().st_mtime:
                log.warning('================================================')
                log.warning(f'{proto_file} is newer than {generated_file}.')
            else:
                # We thought that generated protobuf definitions were out of
                # date, but it doesn't actually look like that's the case.
                # Raise the exception normally.
                raise

            log.warning('A likely fix for this fatal exception is to run the')
            log.warning('`build` target of orbitx/Makefile, or at least')
            log.warning('copy-pasting the contents of the `build` target and')
            log.warning('running it in your shell.')
            log.warning('You\'ll have to do this every time you change')
            log.warning(str(proto_file))
            log.warning('================================================')

        raise


if __name__ == '__main__':
    main()

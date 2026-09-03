#!/usr/bin/env python3
"""
Virtual CHIP-8 Emu 0.1
Python 3.14+ / pygame-ce / 60 FPS

A clean-room CHIP-8 emulator with a blue, mGBA-inspired desktop interface.
No external graphics, fonts, configuration, or audio files are required.

Install:
    python3.14 -m pip install pygame-ce

Run:
    python3.14 chip8emu.py
    python3.14 chip8emu.py path/to/game.ch8
    python3.14 chip8emu.py --self-test

Keyboard -> CHIP-8 keypad:
    1 2 3 4     1 2 3 C
    Q W E R  =  4 5 6 D
    A S D F     7 8 9 E
    Z X C V     A 0 B F
"""

from __future__ import annotations

import array
import os
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


APP_NAME = "Virtual CHIP-8 Emu 0.1"
WINDOW_TITLE = f"{APP_NAME} | Python 3.14 | 60 FPS | Audio ON"
FPS = 60
CPU_HZ = 700
FILES_OFF = True

RAM_SIZE = 4096
PROGRAM_START = 0x200
FONT_START = 0x050
DISPLAY_WIDTH = 64
DISPLAY_HEIGHT = 32

# Standard 4x5 CHIP-8 hexadecimal font, stored at 0x050.
FONT_DATA = bytes(
    [
        0xF0, 0x90, 0x90, 0x90, 0xF0,  # 0
        0x20, 0x60, 0x20, 0x20, 0x70,  # 1
        0xF0, 0x10, 0xF0, 0x80, 0xF0,  # 2
        0xF0, 0x10, 0xF0, 0x10, 0xF0,  # 3
        0x90, 0x90, 0xF0, 0x10, 0x10,  # 4
        0xF0, 0x80, 0xF0, 0x10, 0xF0,  # 5
        0xF0, 0x80, 0xF0, 0x90, 0xF0,  # 6
        0xF0, 0x10, 0x20, 0x40, 0x40,  # 7
        0xF0, 0x90, 0xF0, 0x90, 0xF0,  # 8
        0xF0, 0x90, 0xF0, 0x10, 0xF0,  # 9
        0xF0, 0x90, 0xF0, 0x90, 0x90,  # A
        0xE0, 0x90, 0xE0, 0x90, 0xE0,  # B
        0xF0, 0x80, 0x80, 0x80, 0xF0,  # C
        0xE0, 0x90, 0x90, 0x90, 0xE0,  # D
        0xF0, 0x80, 0xF0, 0x80, 0xF0,  # E
        0xF0, 0x80, 0xF0, 0x80, 0x80,  # F
    ]
)

# Original tiny ROM: draws 0-F using the emulated font, sounds a short beep,
# then idles. This makes the emulator immediately testable without a download.
BUILTIN_DIAGNOSTIC_ROM = bytes(
    [
        0x00, 0xE0,  # CLS
        0x60, 0x00,  # V0 = digit 0
        0x61, 0x00,  # V1 = x 0
        0x62, 0x00,  # V2 = y 0
        0xF0, 0x29,  # I = font(V0)
        0xD1, 0x25,  # draw V0 at V1,V2
        0x71, 0x08,  # x += 8
        0x70, 0x01,  # digit += 1
        0x30, 0x08,  # first row finished?
        0x12, 0x08,  # loop first row
        0x61, 0x00,  # x = 0
        0x62, 0x08,  # y = 8
        0xF0, 0x29,  # I = font(V0)
        0xD1, 0x25,  # draw V0 at V1,V2
        0x71, 0x08,  # x += 8
        0x70, 0x01,  # digit += 1
        0x30, 0x10,  # all 16 finished?
        0x12, 0x18,  # loop second row
        0x63, 0x08,  # V3 = 8 ticks
        0xF3, 0x18,  # sound timer = V3
        0x12, 0x28,  # idle forever
    ]
)


class Chip8Error(RuntimeError):
    """Raised when a ROM executes invalid or unsafe CHIP-8 state."""


@dataclass(slots=True)
class CompatibilityProfile:
    name: str
    shift_uses_vy: bool
    load_store_increment_i: bool
    jump_uses_vx: bool
    draw_wrap: bool


ORIGINAL_PROFILE = CompatibilityProfile("CHIP-8", True, True, False, True)
CHIP48_PROFILE = CompatibilityProfile("CHIP-48", False, False, True, False)


class Chip8:
    """Complete classic CHIP-8 virtual machine."""

    def __init__(self, profile: CompatibilityProfile = ORIGINAL_PROFILE) -> None:
        self.profile = profile
        self.rng = random.Random()
        self.memory = bytearray(RAM_SIZE)
        self.v = bytearray(16)
        self.i = 0
        self.pc = PROGRAM_START
        self.stack: list[int] = []
        self.delay_timer = 0
        self.sound_timer = 0
        self.keys = [False] * 16
        self.display = bytearray(DISPLAY_WIDTH * DISPLAY_HEIGHT)
        self.draw_pending = True
        self.waiting_for_key: int | None = None
        self.last_opcode = 0
        self.instructions = 0
        self.rom = b""
        self.reset()

    def reset(self) -> None:
        self.memory[:] = bytes(RAM_SIZE)
        self.memory[FONT_START : FONT_START + len(FONT_DATA)] = FONT_DATA
        self.v[:] = bytes(16)
        self.i = 0
        self.pc = PROGRAM_START
        self.stack.clear()
        self.delay_timer = 0
        self.sound_timer = 0
        self.keys[:] = [False] * 16
        self.display[:] = bytes(DISPLAY_WIDTH * DISPLAY_HEIGHT)
        self.draw_pending = True
        self.waiting_for_key = None
        self.last_opcode = 0
        self.instructions = 0
        if self.rom:
            self._copy_rom(self.rom)

    def _copy_rom(self, rom: bytes) -> None:
        end = PROGRAM_START + len(rom)
        if end > RAM_SIZE:
            raise Chip8Error(
                f"ROM is {len(rom)} bytes; maximum is {RAM_SIZE - PROGRAM_START}."
            )
        self.memory[PROGRAM_START:end] = rom

    def load_rom(self, rom: bytes) -> None:
        if not rom:
            raise Chip8Error("The selected ROM is empty.")
        if len(rom) > RAM_SIZE - PROGRAM_START:
            raise Chip8Error(
                f"ROM is {len(rom)} bytes; maximum is {RAM_SIZE - PROGRAM_START}."
            )
        self.rom = bytes(rom)
        self.reset()

    def set_profile(self, profile: CompatibilityProfile) -> None:
        self.profile = profile

    def set_key(self, key: int, pressed: bool) -> None:
        if 0 <= key < 16:
            self.keys[key] = pressed
            if pressed and self.waiting_for_key is not None:
                self.v[self.waiting_for_key] = key
                self.waiting_for_key = None

    def tick_timers(self) -> None:
        if self.delay_timer:
            self.delay_timer -= 1
        if self.sound_timer:
            self.sound_timer -= 1

    def step(self) -> bool:
        """Execute one instruction. Returns False while FX0A is waiting."""
        if self.waiting_for_key is not None:
            return False
        if self.pc < PROGRAM_START or self.pc + 1 >= RAM_SIZE:
            raise Chip8Error(f"Program counter escaped memory: 0x{self.pc:03X}")

        opcode = (self.memory[self.pc] << 8) | self.memory[self.pc + 1]
        self.last_opcode = opcode
        self.pc += 2
        self.instructions += 1

        nnn = opcode & 0x0FFF
        nn = opcode & 0x00FF
        n = opcode & 0x000F
        x = (opcode >> 8) & 0x0F
        y = (opcode >> 4) & 0x0F
        family = opcode >> 12

        if opcode == 0x00E0:
            self.display[:] = bytes(DISPLAY_WIDTH * DISPLAY_HEIGHT)
            self.draw_pending = True
        elif opcode == 0x00EE:
            if not self.stack:
                raise Chip8Error("Stack underflow on 00EE.")
            self.pc = self.stack.pop()
        elif family == 0x0:
            # 0NNN called an RCA 1802 routine. Modern interpreters ignore it.
            pass
        elif family == 0x1:
            self.pc = nnn
        elif family == 0x2:
            if len(self.stack) >= 16:
                raise Chip8Error("Stack overflow on 2NNN.")
            self.stack.append(self.pc)
            self.pc = nnn
        elif family == 0x3:
            if self.v[x] == nn:
                self.pc += 2
        elif family == 0x4:
            if self.v[x] != nn:
                self.pc += 2
        elif family == 0x5 and n == 0:
            if self.v[x] == self.v[y]:
                self.pc += 2
        elif family == 0x6:
            self.v[x] = nn
        elif family == 0x7:
            self.v[x] = (self.v[x] + nn) & 0xFF
        elif family == 0x8:
            self._execute_8xy(opcode, x, y, n)
        elif family == 0x9 and n == 0:
            if self.v[x] != self.v[y]:
                self.pc += 2
        elif family == 0xA:
            self.i = nnn
        elif family == 0xB:
            base_register = x if self.profile.jump_uses_vx else 0
            self.pc = nnn + self.v[base_register]
        elif family == 0xC:
            self.v[x] = self.rng.randrange(256) & nn
        elif family == 0xD:
            self._draw_sprite(self.v[x], self.v[y], n)
        elif family == 0xE and nn == 0x9E:
            key = self.v[x]
            if key < 16 and self.keys[key]:
                self.pc += 2
        elif family == 0xE and nn == 0xA1:
            key = self.v[x]
            if key >= 16 or not self.keys[key]:
                self.pc += 2
        elif family == 0xF:
            self._execute_fx(opcode, x, nn)
        else:
            raise Chip8Error(f"Unknown opcode 0x{opcode:04X} at 0x{self.pc - 2:03X}")

        if self.pc < 0 or self.pc >= RAM_SIZE:
            raise Chip8Error(f"Jump escaped memory: 0x{self.pc:04X}")
        return True

    def _execute_8xy(self, opcode: int, x: int, y: int, n: int) -> None:
        if n == 0x0:
            self.v[x] = self.v[y]
        elif n == 0x1:
            self.v[x] |= self.v[y]
        elif n == 0x2:
            self.v[x] &= self.v[y]
        elif n == 0x3:
            self.v[x] ^= self.v[y]
        elif n == 0x4:
            total = self.v[x] + self.v[y]
            self.v[0xF] = 1 if total > 0xFF else 0
            self.v[x] = total & 0xFF
        elif n == 0x5:
            vx, vy = self.v[x], self.v[y]
            self.v[0xF] = 1 if vx >= vy else 0
            self.v[x] = (vx - vy) & 0xFF
        elif n == 0x6:
            source = self.v[y] if self.profile.shift_uses_vy else self.v[x]
            self.v[0xF] = source & 1
            self.v[x] = source >> 1
        elif n == 0x7:
            vx, vy = self.v[x], self.v[y]
            self.v[0xF] = 1 if vy >= vx else 0
            self.v[x] = (vy - vx) & 0xFF
        elif n == 0xE:
            source = self.v[y] if self.profile.shift_uses_vy else self.v[x]
            self.v[0xF] = (source >> 7) & 1
            self.v[x] = (source << 1) & 0xFF
        else:
            raise Chip8Error(f"Unknown opcode 0x{opcode:04X}")

    def _execute_fx(self, opcode: int, x: int, nn: int) -> None:
        if nn == 0x07:
            self.v[x] = self.delay_timer
        elif nn == 0x0A:
            pressed = next((k for k, down in enumerate(self.keys) if down), None)
            if pressed is None:
                self.waiting_for_key = x
            else:
                self.v[x] = pressed
        elif nn == 0x15:
            self.delay_timer = self.v[x]
        elif nn == 0x18:
            self.sound_timer = self.v[x]
        elif nn == 0x1E:
            total = self.i + self.v[x]
            self.v[0xF] = 1 if total > 0x0FFF else 0
            self.i = total & 0x0FFF
        elif nn == 0x29:
            self.i = FONT_START + (self.v[x] & 0x0F) * 5
        elif nn == 0x33:
            self._require_memory(self.i, 3)
            value = self.v[x]
            self.memory[self.i] = value // 100
            self.memory[self.i + 1] = (value // 10) % 10
            self.memory[self.i + 2] = value % 10
        elif nn == 0x55:
            self._require_memory(self.i, x + 1)
            self.memory[self.i : self.i + x + 1] = self.v[: x + 1]
            if self.profile.load_store_increment_i:
                self.i = (self.i + x + 1) & 0x0FFF
        elif nn == 0x65:
            self._require_memory(self.i, x + 1)
            self.v[: x + 1] = self.memory[self.i : self.i + x + 1]
            if self.profile.load_store_increment_i:
                self.i = (self.i + x + 1) & 0x0FFF
        else:
            raise Chip8Error(f"Unknown opcode 0x{opcode:04X}")

    def _require_memory(self, start: int, length: int) -> None:
        if start < 0 or start + length > RAM_SIZE:
            raise Chip8Error(
                f"Memory access 0x{start:03X}..0x{start + length - 1:03X} is invalid."
            )

    def _draw_sprite(self, x_pos: int, y_pos: int, rows: int) -> None:
        if rows == 0:
            # DXY0 is a Super-CHIP 16x16 opcode and is deliberately not guessed.
            raise Chip8Error("DXY0 requires Super-CHIP mode (not classic CHIP-8).")
        self._require_memory(self.i, rows)
        self.v[0xF] = 0
        for row in range(rows):
            sprite = self.memory[self.i + row]
            for bit in range(8):
                if not (sprite & (0x80 >> bit)):
                    continue
                px = x_pos + bit
                py = y_pos + row
                if self.profile.draw_wrap:
                    px %= DISPLAY_WIDTH
                    py %= DISPLAY_HEIGHT
                elif px >= DISPLAY_WIDTH or py >= DISPLAY_HEIGHT:
                    continue
                index = py * DISPLAY_WIDTH + px
                if self.display[index]:
                    self.v[0xF] = 1
                self.display[index] ^= 1
        self.draw_pending = True


def run_core_self_test(verbose: bool = True) -> bool:
    """Headless smoke test for arithmetic, calls, drawing, BCD, and timers."""
    vm = Chip8()
    vm.load_rom(BUILTIN_DIAGNOSTIC_ROM)
    for _ in range(170):
        vm.step()
    assert sum(vm.display) > 80, "diagnostic ROM did not draw"
    assert vm.sound_timer == 8, "sound timer was not set"

    # LD/add/carry/subtract/call/return and BCD.
    test_rom = bytes(
        [
            0x61, 0xFE,  # V1 = 254
            0x62, 0x04,  # V2 = 4
            0x81, 0x24,  # V1 += V2 => 2, carry
            0x22, 0x10,  # call subroutine
            0xA3, 0x00,  # I = 0x300
            0x63, 0xEA,  # V3 = 234
            0xF3, 0x33,  # BCD V3
            0x12, 0x0E,  # stop loop
            0x71, 0x01,  # subroutine: V1 += 1
            0x00, 0xEE,  # return
        ]
    )
    vm.load_rom(test_rom)
    for _ in range(9):
        vm.step()
    assert vm.v[1] == 3 and vm.v[0xF] == 1, "ALU/call test failed"
    assert vm.memory[0x300:0x303] == bytes([2, 3, 4]), "BCD test failed"
    assert not vm.stack, "call stack did not unwind"
    if verbose:
        print("Virtual CHIP-8 Emu 0.1 core self-test: PASS")
    return True


# UI dependencies are optional at import time so --self-test remains headless.
try:
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    import pygame
except ImportError:
    pygame = None  # type: ignore[assignment]


class AudioEngine:
    """Procedural square-wave buzzer; never reads an external audio file."""

    def __init__(self) -> None:
        self.available = False
        self.playing = False
        self.sound = None
        if pygame is None:
            return
        try:
            if pygame.mixer.get_init() is None:
                pygame.mixer.init(frequency=44_100, size=-16, channels=2, buffer=512)
            init = pygame.mixer.get_init()
            if init is None:
                return
            sample_rate, _format, channels = init
            samples = array.array("h")
            frames = max(1, sample_rate // 55)  # seamless short buffer
            period = max(2, sample_rate // 440)
            for frame in range(frames):
                value = 5_500 if (frame % period) < period // 2 else -5_500
                for _ in range(channels):
                    samples.append(value)
            if sys.byteorder != "little":
                samples.byteswap()
            self.sound = pygame.mixer.Sound(buffer=samples.tobytes())
            self.sound.set_volume(0.22)
            self.available = True
        except (pygame.error, RuntimeError, ValueError):
            self.available = False

    def update(self, should_play: bool) -> None:
        should_play = should_play and self.available
        if should_play and not self.playing and self.sound is not None:
            self.sound.play(loops=-1)
            self.playing = True
        elif not should_play and self.playing:
            self.stop()

    def stop(self) -> None:
        if self.sound is not None:
            self.sound.stop()
        self.playing = False


@dataclass(slots=True)
class UIButton:
    label: str
    rect: object
    action: Callable[[], None]
    active: bool = False


class EmulatorApp:
    WIDTH = 1120
    HEIGHT = 700

    BG = (7, 17, 48)
    PANEL = (12, 30, 72)
    PANEL_2 = (17, 41, 92)
    BLACK = (2, 5, 13)
    BLUE = (65, 145, 255)
    CYAN = (78, 218, 255)
    PALE = (174, 221, 255)
    DIM = (80, 122, 178)
    BORDER = (30, 94, 178)
    SCREEN_OFF = (1, 8, 24)
    SCREEN_ON = (56, 200, 255)
    WARNING = (255, 184, 72)
    ERROR = (255, 99, 132)

    def __init__(self, initial_rom: str | None = None) -> None:
        if pygame is None:
            raise SystemExit(
                "pygame-ce is required. Install it with:\n"
                "  python3.14 -m pip install pygame-ce"
            )
        pygame.mixer.pre_init(frequency=44_100, size=-16, channels=2, buffer=512)
        pygame.init()
        pygame.display.set_caption(WINDOW_TITLE)
        self.window = pygame.display.set_mode(
            (self.WIDTH, self.HEIGHT), pygame.RESIZABLE | pygame.DOUBLEBUF
        )
        self.clock = pygame.time.Clock()
        self.font_small = pygame.font.Font(None, 18)
        self.font = pygame.font.Font(None, 22)
        self.font_bold = pygame.font.Font(None, 26)
        self.font_title = pygame.font.Font(None, 30)
        self.vm = Chip8()
        self.vm.load_rom(BUILTIN_DIAGNOSTIC_ROM)
        self.audio = AudioEngine()
        self.audio_enabled = True
        self.paused = False
        self.running = True
        self.scanlines = True
        self.active_menu: str | None = None
        self.show_about = False
        self.rom_name = "Built-in diagnostic"
        self.last_message = "READY — Load a .ch8 ROM or drag it onto the window"
        self.message_color = self.CYAN
        self.message_until = 0
        self.cpu_accumulator = 0.0
        self.timer_accumulator = 0.0
        self.measured_ips = 0
        self._ips_counter = 0
        self._ips_time = 0.0
        self.menu_buttons: list[UIButton] = []
        self.toolbar_buttons: list[UIButton] = []
        self.dropdown_buttons: list[UIButton] = []
        self.show_file_browser = False
        self.browser_path = Path.cwd()
        self.last_browser_path = Path.cwd()
        self.browser_entries: list[tuple[str, Path, str]] = []
        self.browser_selection = 0
        self.browser_scroll = 0
        self.browser_visible = 14
        self.browser_rects: list[object] = []

        if initial_rom:
            self.load_rom_path(initial_rom)

    def message(self, text: str, color: tuple[int, int, int] | None = None) -> None:
        self.last_message = text
        self.message_color = color or self.CYAN
        self.message_until = pygame.time.get_ticks() + 5000

    def load_rom_path(self, filename: str) -> None:
        try:
            path = Path(filename).expanduser()
            rom = path.read_bytes()
            self.vm.load_rom(rom)
            self.rom_name = path.name
            self.paused = False
            self.cpu_accumulator = 0.0
            self.timer_accumulator = 0.0
            self.message(f"LOADED {path.name} — {len(rom)} bytes")
        except (OSError, Chip8Error) as exc:
            self.paused = True
            self.message(f"LOAD ERROR: {exc}", self.ERROR)

    def open_rom_dialog(self) -> None:
        """Pygame-native ROM browser — never uses tkinter (crashes on macOS 27+)."""
        self.active_menu = None
        self.show_file_browser = True
        self.paused = True
        start = self.last_browser_path
        if start.is_file():
            start = start.parent
        if not start.is_dir():
            start = Path.cwd()
        self.browser_path = start
        self.browser_selection = 0
        self.browser_scroll = 0
        self._refresh_browser_entries()
        self.message("BROWSE FOR A .ch8 ROM — ENTER OPEN, ESC CANCEL")

    def close_file_browser(self) -> None:
        self.show_file_browser = False
        self.last_browser_path = self.browser_path

    def _refresh_browser_entries(self) -> None:
        entries: list[tuple[str, Path, str]] = []
        path = self.browser_path
        if path.parent != path:
            entries.append(("..  (parent folder)", path.parent, "dir"))
        try:
            items = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            items = []
        for item in items:
            if item.name.startswith("."):
                continue
            if item.is_dir():
                entries.append((item.name + "/", item, "dir"))
            elif item.suffix.lower() in (".ch8", ".c8", ".rom", ".bin"):
                entries.append((item.name, item, "rom"))
        self.browser_entries = entries
        if self.browser_entries:
            self.browser_selection = min(self.browser_selection, len(self.browser_entries) - 1)
        else:
            self.browser_selection = 0

    def _activate_browser_selection(self) -> None:
        if not self.browser_entries:
            return
        _label, path, kind = self.browser_entries[self.browser_selection]
        if kind == "dir":
            self.browser_path = path
            self.browser_selection = 0
            self.browser_scroll = 0
            self._refresh_browser_entries()
            return
        self.close_file_browser()
        self.load_rom_path(str(path))
        self.last_browser_path = path.parent

    def handle_browser_key(self, event: object, pressed: bool) -> None:
        if not pressed:
            return
        if event.key == pygame.K_ESCAPE:
            self.close_file_browser()
            self.message("OPEN ROM CANCELLED", self.DIM)
        elif event.key == pygame.K_UP:
            self.browser_selection = max(0, self.browser_selection - 1)
            if self.browser_selection < self.browser_scroll:
                self.browser_scroll = self.browser_selection
        elif event.key == pygame.K_DOWN:
            last = max(0, len(self.browser_entries) - 1)
            self.browser_selection = min(last, self.browser_selection + 1)
            if self.browser_selection >= self.browser_scroll + self.browser_visible:
                self.browser_scroll = self.browser_selection - self.browser_visible + 1
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            self._activate_browser_selection()
        elif event.key == pygame.K_BACKSPACE:
            if self.browser_path.parent != self.browser_path:
                self.browser_path = self.browser_path.parent
                self.browser_selection = 0
                self.browser_scroll = 0
                self._refresh_browser_entries()

    def handle_browser_click(self, pos: tuple[int, int]) -> bool:
        for offset, rect in enumerate(self.browser_rects):
            if rect.collidepoint(pos):
                self.browser_selection = self.browser_scroll + offset
                self._activate_browser_selection()
                return True
        return False

    def draw_file_browser(self) -> None:
        if not self.show_file_browser:
            return
        width, height = self.window.get_size()
        shade = pygame.Surface((width, height), pygame.SRCALPHA)
        shade.fill((0, 0, 12, 200))
        self.window.blit(shade, (0, 0))

        panel_w, panel_h = min(680, width - 80), min(520, height - 80)
        panel = pygame.Rect(0, 0, panel_w, panel_h)
        panel.center = (width // 2, height // 2)
        pygame.draw.rect(self.window, self.PANEL, panel, border_radius=8)
        pygame.draw.rect(self.window, self.CYAN, panel, 2, border_radius=8)

        title = self.font_bold.render("OPEN CHIP-8 ROM", True, self.CYAN)
        self.window.blit(title, (panel.x + 16, panel.y + 14))
        path_text = str(self.browser_path)
        if len(path_text) > 72:
            path_text = "…" + path_text[-71:]
        self.text(self.window, path_text, (panel.x + 16, panel.y + 44), self.PALE, self.font_small)
        self.text(
            self.window,
            "UP/DOWN SELECT  ENTER OPEN  BACKSPACE UP  ESC CANCEL",
            (panel.x + 16, panel.y + 66),
            self.DIM,
            self.font_small,
        )

        list_top = panel.y + 92
        row_h = 24
        list_rect = pygame.Rect(panel.x + 12, list_top, panel_w - 24, self.browser_visible * row_h)
        pygame.draw.rect(self.window, self.BLACK, list_rect, border_radius=4)
        pygame.draw.rect(self.window, self.BORDER, list_rect, 1, border_radius=4)

        self.browser_rects = []
        visible = self.browser_entries[self.browser_scroll : self.browser_scroll + self.browser_visible]
        for row, (label, _path, kind) in enumerate(visible):
            idx = self.browser_scroll + row
            y = list_top + row * row_h
            row_rect = pygame.Rect(list_rect.x + 2, y + 1, list_rect.width - 4, row_h - 2)
            self.browser_rects.append(row_rect)
            if idx == self.browser_selection:
                pygame.draw.rect(self.window, (8, 28, 62), row_rect, border_radius=3)
            prefix = "[DIR] " if kind == "dir" else "[ROM] "
            color = self.CYAN if kind == "rom" else self.PALE
            if len(label) > 58:
                label = label[:55] + "…"
            self.text(self.window, prefix + label, (row_rect.x + 8, y + 4), color, self.font_small)

        if not self.browser_entries:
            self.text(
                self.window,
                "No ROM files here — open a folder or use drag & drop",
                (list_rect.x + 12, list_top + 8),
                self.DIM,
                self.font_small,
            )

        if len(self.browser_entries) > self.browser_visible:
            info = f"{self.browser_selection + 1}/{len(self.browser_entries)}"
            rendered = self.font_small.render(info, True, self.DIM)
            self.window.blit(rendered, (panel.right - rendered.get_width() - 16, panel.bottom - 28))

    def reset(self) -> None:
        self.vm.reset()
        self.paused = False
        self.message(f"RESET — {self.rom_name}")

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.active_menu = None
        self.message("PAUSED" if self.paused else "RUNNING")

    def single_step(self) -> None:
        self.paused = True
        self.active_menu = None
        try:
            self.vm.step()
            self.message(f"STEP — opcode 0x{self.vm.last_opcode:04X}")
        except Chip8Error as exc:
            self.message(f"CPU ERROR: {exc}", self.ERROR)

    def toggle_audio(self) -> None:
        self.audio_enabled = not self.audio_enabled
        self.active_menu = None
        state = "ON" if self.audio_enabled else "OFF"
        if self.audio_enabled and not self.audio.available:
            self.message("AUDIO ON, BUT NO SDL AUDIO DEVICE IS AVAILABLE", self.WARNING)
        else:
            self.message(f"AUDIO {state}")

    def toggle_profile(self) -> None:
        profile = CHIP48_PROFILE if self.vm.profile is ORIGINAL_PROFILE else ORIGINAL_PROFILE
        self.vm.set_profile(profile)
        self.message(f"COMPATIBILITY PROFILE: {profile.name}")

    def toggle_scanlines(self) -> None:
        self.scanlines = not self.scanlines
        self.active_menu = None
        self.message(f"SCANLINES {'ON' if self.scanlines else 'OFF'}")

    def toggle_about(self) -> None:
        self.show_about = not self.show_about
        self.active_menu = None

    def _set_menu(self, name: str) -> Callable[[], None]:
        def action() -> None:
            self.active_menu = None if self.active_menu == name else name
        return action

    def handle_key(self, event: object, pressed: bool) -> None:
        key_map = {
            pygame.K_1: 0x1, pygame.K_2: 0x2, pygame.K_3: 0x3, pygame.K_4: 0xC,
            pygame.K_q: 0x4, pygame.K_w: 0x5, pygame.K_e: 0x6, pygame.K_r: 0xD,
            pygame.K_a: 0x7, pygame.K_s: 0x8, pygame.K_d: 0x9, pygame.K_f: 0xE,
            pygame.K_z: 0xA, pygame.K_x: 0x0, pygame.K_c: 0xB, pygame.K_v: 0xF,
        }
        chip_key = key_map.get(event.key)
        if chip_key is not None:
            self.vm.set_key(chip_key, pressed)
        if not pressed:
            return
        if event.key == pygame.K_F2:
            self.open_rom_dialog()
        elif event.key == pygame.K_F5:
            self.reset()
        elif event.key == pygame.K_F6:
            self.toggle_pause()
        elif event.key == pygame.K_F7:
            self.single_step()
        elif event.key == pygame.K_F8:
            self.toggle_audio()
        elif event.key == pygame.K_ESCAPE:
            if self.show_file_browser:
                self.close_file_browser()
                self.message("OPEN ROM CANCELLED", self.DIM)
            elif self.show_about:
                self.show_about = False
            elif self.active_menu:
                self.active_menu = None
            else:
                self.toggle_pause()

    def handle_click(self, pos: tuple[int, int]) -> None:
        if self.show_file_browser:
            self.handle_browser_click(pos)
            return
        if self.show_about:
            self.show_about = False
            return
        # Drop-down entries take priority over everything behind them.
        for button in self.dropdown_buttons:
            if button.rect.collidepoint(pos):
                button.action()
                return
        for button in self.menu_buttons:
            if button.rect.collidepoint(pos):
                button.action()
                return
        for button in self.toolbar_buttons:
            if button.rect.collidepoint(pos):
                button.action()
                return
        self.active_menu = None

    def pump_events(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
            elif event.type == pygame.KEYDOWN:
                if self.show_file_browser:
                    self.handle_browser_key(event, True)
                else:
                    self.handle_key(event, True)
            elif event.type == pygame.KEYUP:
                if not self.show_file_browser:
                    self.handle_key(event, False)
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                self.handle_click(event.pos)
            elif event.type == pygame.DROPFILE:
                self.load_rom_path(event.file)

    def update(self, dt: float) -> None:
        if self.paused:
            self.audio.update(False)
            return

        dt = min(dt, 0.1)
        self.cpu_accumulator += CPU_HZ * dt
        cycles = min(int(self.cpu_accumulator), 100)
        self.cpu_accumulator -= cycles
        try:
            for _ in range(cycles):
                if self.vm.step():
                    self._ips_counter += 1
        except Chip8Error as exc:
            self.paused = True
            self.message(f"CPU ERROR: {exc}", self.ERROR)

        self.timer_accumulator += dt
        while self.timer_accumulator >= 1.0 / 60.0:
            self.vm.tick_timers()
            self.timer_accumulator -= 1.0 / 60.0

        self._ips_time += dt
        if self._ips_time >= 1.0:
            self.measured_ips = round(self._ips_counter / self._ips_time)
            self._ips_counter = 0
            self._ips_time = 0.0
        self.audio.update(self.audio_enabled and self.vm.sound_timer > 0)

    def text(
        self,
        surface: object,
        value: str,
        pos: tuple[int, int],
        color: tuple[int, int, int] | None = None,
        font: object | None = None,
    ) -> object:
        rendered = (font or self.font).render(value, True, color or self.BLUE)
        surface.blit(rendered, pos)
        return rendered

    def button(
        self,
        surface: object,
        label: str,
        rect: object,
        active: bool = False,
        small: bool = False,
    ) -> None:
        fill = (8, 28, 62) if active else self.BLACK
        border = self.CYAN if active else self.BORDER
        pygame.draw.rect(surface, fill, rect, border_radius=4)
        pygame.draw.rect(surface, border, rect, 1, border_radius=4)
        font = self.font_small if small else self.font
        rendered = font.render(label, True, self.CYAN if active else self.BLUE)
        surface.blit(rendered, rendered.get_rect(center=rect.center))

    def draw_menu_bar(self) -> None:
        width = self.window.get_width()
        pygame.draw.rect(self.window, self.BLACK, (0, 0, width, 34))
        pygame.draw.line(self.window, self.BORDER, (0, 33), (width, 33))
        self.text(self.window, "VIRTUAL CHIP-8", (14, 8), self.CYAN, self.font_bold)
        menus = [("FILE", 190), ("EMULATION", 252), ("AUDIO", 360), ("VIEW", 430), ("HELP", 492)]
        self.menu_buttons.clear()
        for name, x in menus:
            rect = pygame.Rect(x, 3, max(58, len(name) * 10 + 18), 28)
            self.menu_buttons.append(UIButton(name, rect, self._set_menu(name), self.active_menu == name))
            self.button(self.window, name, rect, self.active_menu == name, small=True)
        badge = self.font_small.render("60 FPS  •  AUDIO ON", True, self.CYAN)
        self.window.blit(badge, (width - badge.get_width() - 14, 9))

    def draw_toolbar(self) -> None:
        width = self.window.get_width()
        pygame.draw.rect(self.window, self.PANEL, (0, 34, width, 64))
        pygame.draw.line(self.window, self.BORDER, (0, 97), (width, 97))
        specs = [
            ("OPEN ROM  F2", self.open_rom_dialog, 132),
            ("RESET  F5", self.reset, 112),
            (("RUN" if self.paused else "PAUSE") + "  F6", self.toggle_pause, 112),
            ("STEP  F7", self.single_step, 102),
            (f"AUDIO {'ON' if self.audio_enabled else 'OFF'}  F8", self.toggle_audio, 126),
            (f"PROFILE: {self.vm.profile.name}", self.toggle_profile, 152),
        ]
        self.toolbar_buttons.clear()
        x = 14
        for label, action, button_width in specs:
            rect = pygame.Rect(x, 48, button_width, 36)
            active = ("AUDIO" in label and self.audio_enabled) or ("PAUSE" in label and not self.paused)
            self.toolbar_buttons.append(UIButton(label, rect, action, active))
            self.button(self.window, label, rect, active, small=True)
            x += button_width + 9

    def draw_display(self) -> tuple[object, int]:
        width, height = self.window.get_size()
        side_width = 250 if width >= 850 else 0
        available_width = width - side_width - 44
        available_height = height - 98 - 92
        scale = max(2, min(14, available_width // DISPLAY_WIDTH, available_height // DISPLAY_HEIGHT))
        screen_w = DISPLAY_WIDTH * scale
        screen_h = DISPLAY_HEIGHT * scale
        x = 18 + max(0, (available_width - screen_w) // 2)
        y = 110 + max(0, (available_height - screen_h) // 2)
        border = pygame.Rect(x - 7, y - 7, screen_w + 14, screen_h + 14)
        pygame.draw.rect(self.window, self.BLACK, border, border_radius=5)
        pygame.draw.rect(self.window, self.BORDER, border, 2, border_radius=5)

        native = pygame.Surface((DISPLAY_WIDTH, DISPLAY_HEIGHT))
        native.fill(self.SCREEN_OFF)
        pixel_array = pygame.PixelArray(native)
        for index, lit in enumerate(self.vm.display):
            if lit:
                pixel_array[index % DISPLAY_WIDTH, index // DISPLAY_WIDTH] = self.SCREEN_ON
        del pixel_array
        scaled = pygame.transform.scale(native, (screen_w, screen_h))
        self.window.blit(scaled, (x, y))

        if self.scanlines and scale >= 4:
            scan = pygame.Surface((screen_w, screen_h), pygame.SRCALPHA)
            for sy in range(scale - 1, screen_h, scale):
                pygame.draw.line(scan, (0, 0, 20, 46), (0, sy), (screen_w, sy))
            self.window.blit(scan, (x, y))
        return pygame.Rect(x, y, screen_w, screen_h), side_width

    def draw_side_panel(self, display_rect: object, side_width: int) -> None:
        if not side_width:
            return
        width, height = self.window.get_size()
        x = width - side_width + 4
        rect = pygame.Rect(x, 110, side_width - 18, height - 162)
        pygame.draw.rect(self.window, self.PANEL, rect, border_radius=5)
        pygame.draw.rect(self.window, self.BORDER, rect, 1, border_radius=5)
        self.text(self.window, "CPU MONITOR", (x + 14, 124), self.CYAN, self.font_bold)
        self.text(self.window, f"ROM   {self.rom_name[:19]}", (x + 14, 156), self.PALE, self.font_small)
        self.text(self.window, f"PC    0x{self.vm.pc:03X}", (x + 14, 178), self.BLUE, self.font_small)
        self.text(self.window, f"I     0x{self.vm.i:03X}", (x + 14, 198), self.BLUE, self.font_small)
        self.text(self.window, f"OP    0x{self.vm.last_opcode:04X}", (x + 14, 218), self.BLUE, self.font_small)
        self.text(self.window, f"DT/ST {self.vm.delay_timer:02X}/{self.vm.sound_timer:02X}", (x + 14, 238), self.BLUE, self.font_small)
        self.text(self.window, f"IPS   {self.measured_ips:4d}", (x + 14, 258), self.BLUE, self.font_small)
        self.text(self.window, f"STACK {len(self.vm.stack):02d}/16", (x + 14, 278), self.BLUE, self.font_small)

        self.text(self.window, "REGISTERS", (x + 14, 310), self.CYAN, self.font_bold)
        for row in range(4):
            values = "  ".join(
                f"V{column + row * 4:X}:{self.vm.v[column + row * 4]:02X}"
                for column in range(4)
            )
            self.text(self.window, values, (x + 14, 340 + row * 22), self.PALE, self.font_small)

        self.text(self.window, "KEYPAD", (x + 14, 440), self.CYAN, self.font_bold)
        keyboard = ["1 2 3 4  →  1 2 3 C", "Q W E R  →  4 5 6 D", "A S D F  →  7 8 9 E", "Z X C V  →  A 0 B F"]
        for row, line in enumerate(keyboard):
            self.text(self.window, line, (x + 14, 472 + row * 21), self.PALE, self.font_small)
        self.text(self.window, "Drag & drop ROMs", (x + 14, rect.bottom - 30), self.DIM, self.font_small)

    def draw_dropdown(self) -> None:
        self.dropdown_buttons.clear()
        if not self.active_menu:
            return
        definitions: dict[str, tuple[int, list[tuple[str, Callable[[], None]]]]] = {
            "FILE": (190, [("Open ROM…       F2", self.open_rom_dialog), ("Quit", self.quit)]),
            "EMULATION": (252, [("Run / Pause    F6", self.toggle_pause), ("Reset          F5", self.reset), ("Single Step    F7", self.single_step)]),
            "AUDIO": (360, [(f"Audio {'ON' if self.audio_enabled else 'OFF'}       F8", self.toggle_audio)]),
            "VIEW": (430, [(f"Scanlines {'ON' if self.scanlines else 'OFF'}", self.toggle_scanlines)]),
            "HELP": (492, [("About Virtual CHIP-8", self.toggle_about)]),
        }
        x, items = definitions[self.active_menu]
        menu_width = 205
        height = len(items) * 34 + 8
        panel = pygame.Rect(x, 34, menu_width, height)
        pygame.draw.rect(self.window, self.BLACK, panel)
        pygame.draw.rect(self.window, self.CYAN, panel, 1)
        for row, (label, action) in enumerate(items):
            rect = pygame.Rect(x + 4, 38 + row * 34, menu_width - 8, 30)
            self.dropdown_buttons.append(UIButton(label, rect, action))
            self.button(self.window, label, rect, small=True)

    def draw_about(self) -> None:
        if not self.show_about:
            return
        width, height = self.window.get_size()
        shade = pygame.Surface((width, height), pygame.SRCALPHA)
        shade.fill((0, 0, 12, 190))
        self.window.blit(shade, (0, 0))
        rect = pygame.Rect(0, 0, 500, 290)
        rect.center = (width // 2, height // 2)
        pygame.draw.rect(self.window, self.PANEL, rect, border_radius=8)
        pygame.draw.rect(self.window, self.CYAN, rect, 2, border_radius=8)
        title = self.font_title.render(APP_NAME, True, self.CYAN)
        self.window.blit(title, title.get_rect(center=(rect.centerx, rect.top + 48)))
        lines = [
            "Clean-room classic CHIP-8 interpreter",
            "Python 3.14 • pygame-ce • 60 FPS",
            "700 instructions per second • 60 Hz timers",
            "Procedural square-wave audio • no asset files",
            "mGBA-inspired interface; not affiliated with mGBA",
            "Click anywhere or press Esc to close",
        ]
        for index, line in enumerate(lines):
            rendered = self.font.render(line, True, self.PALE if index < 5 else self.DIM)
            self.window.blit(rendered, rendered.get_rect(center=(rect.centerx, rect.top + 96 + index * 30)))

    def draw_status(self) -> None:
        width, height = self.window.get_size()
        pygame.draw.rect(self.window, self.BLACK, (0, height - 40, width, 40))
        pygame.draw.line(self.window, self.BORDER, (0, height - 40), (width, height - 40))
        state = "PAUSED" if self.paused else ("WAITING FOR KEY" if self.vm.waiting_for_key is not None else "RUNNING")
        state_color = self.WARNING if self.paused else self.CYAN
        self.text(self.window, state, (14, height - 29), state_color, self.font_bold)
        message = self.last_message
        if pygame.time.get_ticks() > self.message_until and self.message_until:
            message = f"{self.rom_name}  •  {self.vm.profile.name}  •  {CPU_HZ} Hz CPU"
        self.text(self.window, message[:82], (150, height - 27), self.message_color, self.font_small)
        fps_text = f"{self.clock.get_fps():5.1f} FPS"
        rendered = self.font_small.render(fps_text, True, self.CYAN)
        self.window.blit(rendered, (width - rendered.get_width() - 14, height - 27))

    def draw(self) -> None:
        self.window.fill(self.BG)
        self.draw_menu_bar()
        self.draw_toolbar()
        display_rect, side_width = self.draw_display()
        self.draw_side_panel(display_rect, side_width)
        self.draw_status()
        self.draw_dropdown()
        self.draw_about()
        self.draw_file_browser()
        pygame.display.flip()

    def quit(self) -> None:
        self.running = False

    def run(self) -> int:
        try:
            while self.running:
                dt = self.clock.tick(FPS) / 1000.0
                self.pump_events()
                self.update(dt)
                self.draw()
        finally:
            self.audio.stop()
            pygame.quit()
        return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--self-test" in args:
        run_core_self_test()
        return 0
    initial_rom = next((arg for arg in args if not arg.startswith("-")), None)
    app = EmulatorApp(initial_rom)
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())

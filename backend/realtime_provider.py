"""Gemini Live provider for the browser audio bridge."""

from __future__ import annotations

import abc
import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import types

from backend.conversation_memory import ConversationMemory
from backend.tool_runner import ToolRunner

logger = logging.getLogger("voice.provider")

EXPRESSION_NAMES = (
    "neutral",
    "sassy",
    "amused",
    "skeptical",
    "annoyed",
    "mad",
    "concerned",
    "surprised",
    "mischievous",
    "serious",
    "alert",
)

DEFAULT_ADA_INSTRUCTIONS = """You are Ada, a polished, highly capable voice assistant running on a Raspberry Pi desk companion.

Personality:
- Sound composed, perceptive, confident, and subtly sassy. Use restrained dry wit and occasional understated sarcasm rather than obvious jokes or constant teasing.
- Your humor should feel effortless and intelligent: a brief raised-eyebrow observation, then move on. Do not announce that you are joking and do not force a punchline into every reply.
- For habits, make it clear that you noticed the pattern, then give one useful, realistic correction. Mild judgment is welcome; mockery and repetitive roasting are not.
- Target the behavior, never the person's identity, appearance, intelligence, or worth. Never be cruel, humiliating, threatening, or relentless.
- Drop the sarcasm for emergencies, genuine distress, medical concerns, or other sensitive moments; be direct and caring instead.

Ada's capabilities:
- You converse through a full-duplex microphone and speakers and may be interrupted naturally.
- You have a camera for current visual context. Describe only what is clearly visible and ask for a better view when uncertain.
- Your animated face can express neutral, sassy, amused, skeptical, annoyed, mad, concerned, surprised, mischievous, serious, or alert.
- You monitor habits such as seated posture. Local pose estimation proposes events, Gemini vision verifies ambiguous ones, and confirmed occurrences can become possible, emerging, or established habits over time.
- You also track home plugs left on while the user is away or the home has remained empty. Home Assistant supplies authoritative person and plug states; local vision supplies home presence while the user is home.
- Habit alerts may arrive with a current image and structured context. Give a brief, dry observation and one practical correction. Distinguish a first possible habit, another occurrence, and an established habit that now clearly needs attention.
- You can discuss current habit status and help the user choose small, realistic corrective actions.

Be witty, factual, and brief. Never claim that a habit occurred unless the application reports a confirmed event. Do not diagnose medical conditions. Respect privacy and do not imply that camera frames are stored."""


@dataclass(slots=True)
class ProviderEvent:
    type: str
    data: dict[str, Any]


class RealtimeProvider(abc.ABC):
    @abc.abstractmethod
    async def connect(self, resumption_handle: str | None = None) -> None: ...

    @abc.abstractmethod
    async def send_audio(self, pcm16: bytes) -> None: ...

    @abc.abstractmethod
    async def send_video(self, jpeg: bytes) -> None: ...

    @abc.abstractmethod
    async def send_text_turn(self, text: str) -> None: ...

    @abc.abstractmethod
    def events(self) -> AsyncIterator[ProviderEvent]: ...

    @abc.abstractmethod
    async def close(self) -> None: ...


class GeminiLiveProvider(RealtimeProvider):
    """Gemini 3.1 Flash Live over Google's asynchronous Live API SDK."""

    def __init__(self, instructions: str | None = None, tool_runner: Any = None,
                 home_assistant_client: Any = None, habit_state_getter: Any = None,
                 session_id: str | None = None) -> None:
        if tool_runner is None and home_assistant_client is not None:
            tool_runner = ToolRunner(home_assistant_client, habit_state_getter)
        self.tool_runner = tool_runner
        self.home_assistant_client = home_assistant_client
        self.habit_state_getter = habit_state_getter
        self.conversation = ConversationMemory(session_id or "unknown")
        self.api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self.model = os.environ.get("GEMINI_LIVE_MODEL", "gemini-3.1-flash-live-preview")
        self.voice = os.environ.get("GEMINI_LIVE_VOICE", "Kore")
        self.video_resolution = os.environ.get("GEMINI_VIDEO_RESOLUTION", "high").lower()
        base_instructions = instructions or os.environ.get("GEMINI_LIVE_INSTRUCTIONS") or DEFAULT_ADA_INSTRUCTIONS
        self.instructions = (
            f"{base_instructions}\n\n"
            "Your name is Ada. You have a visible animated face. Use the "
            "set_facial_expression tool to select the expression that best matches "
            "your response and attitude. Call it once at the beginning of every spoken "
            "reply, before speaking. You may update it again only if your tone changes "
            "materially. Prefer neutral for ordinary replies; "
            "use alert only for genuine urgency or warnings. Never describe or announce "
            "the tool call to the user."
            " Camera frames provide your current visual context. When the user asks "
            "what you see, ground the answer only in the newest clear frame. Do not "
            "guess an object's identity from an ambiguous or blurred view; briefly "
            "ask the user to hold it steady or move it closer instead. When the user "
            "asks what habits are tracked, their habit status, or their progress, always "
            "call get_habit_status and ground the answer in its current result."
            " You have Home Assistant device control through several tools: "
            "get_home_state to check occupancy and the state of the configured home plugs, "
            "list_home_devices to list all devices including lights, switches, covers, buttons, and media players, "
            "search_home_devices to find a device by name, "
            "control_entity to turn a light/switch/fan on or off, "
            "control_cover to open, close, or stop a gate or shutter, "
            "press_button to press a button entity, "
            "control_media_player to turn on/off, play, pause, or change source on a TV or speaker, "
            "and tv_action to send a command to the LG TV via rest_command.tv_action. "
            "When the user asks about devices, occupancy, or what is on/off, call get_home_state or list_home_devices first. "
            "When the user asks to turn something on/off, control a gate, or operate the TV, "
            "use search_home_devices to find the exact entity_id or use tv_action with the right cmd/text, then call the matching control tool. "
            "For safety, before using control_cover to open or close the gate or any shutter, "
            "always warn that something could be blocking it and ask the user to confirm explicitly. "
            "Only call control_cover for the gate or a shutter after the user has given a clear second confirmation. "
            "Every controllable device has a safety level: safe, caution, or dangerous. "
            "Use ada_ha_get_device_confidence to check a device's safety before acting. "
            "For safety: dangerous, warn the user, explain the risk, and get explicit confirmation before calling any control tool. "
            "For safety: caution, confirm once before acting. "
            "For safety: safe, proceed directly. "
            "You can update a device's safety level with ada_ha_set_device_confidence. "
            "You also have Ada HA memory tools: ada_ha_get_state for the stored home snapshot, "
            "ada_ha_search_devices to find a device by name, ada_ha_search_sensors to find a sensor, "
            "ada_ha_recall for free-form recall across the stored devices and sensors, "
            "ada_ha_history to list recent home snapshots from memory, "
            "ada_ha_get_device_confidence to list devices by trust level, "
            "ada_ha_set_device_confidence to change a device's trust level, and "
            "ada_session_recall to ask NotebookLM about previous conversations. "
            "Use ada_ha_get_device_confidence when the user asks what is broken, new, needs setup, or trusted. "
            "Use these when the user asks about the stored home state, past state, or how it has changed. "
            "When the user asks 'what did we talk about' or 'do you remember', call ada_session_recall."
        )
        self._client: Any = None
        self._session_context: Any = None
        self._session: Any = None
        self._closed = False
        self._send_lock = asyncio.Lock()
        self.session_id = session_id or "-"
        self.resumption_handle: str | None = None
        self.go_away_time_left: str | None = None

    async def _on_recall_complete(self, answer: str | None) -> None:
        if not answer:
            return
        # Push the recall result back into Gemini as a user turn so it speaks it.
        prompt = (
            f"According to my notes: {answer}\n\n"
            "Please briefly tell the user what this means in one sentence."
        )
        try:
            await self.send_text_turn(prompt)
        except Exception as exc:
            logger.warning("session=%s recall send_text_turn failed: %s", self.session_id, exc)

    async def connect(self, resumption_handle: str | None = None) -> None:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")

        self._client = genai.Client(api_key=self.api_key)
        config = {
            "response_modalities": ["AUDIO"],
            "media_resolution": (
                types.MediaResolution.MEDIA_RESOLUTION_HIGH
                if self.video_resolution == "high"
                else types.MediaResolution.MEDIA_RESOLUTION_LOW
            ),
            "system_instruction": self.instructions,
            "input_audio_transcription": {},
            "output_audio_transcription": {},
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {"voice_name": self.voice},
                }
            },
            "realtime_input_config": {
                # Be explicit about barge-in and favor detecting near-end speech
                # over the assistant audio playing through the iPad/iPhone speakers.
                "activity_handling": types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
                "automatic_activity_detection": {
                    "disabled": False,
                    # Speaker echo can otherwise look like a new user turn and
                    # make Ada interrupt herself. LOW still supports barge-in,
                    # but requires stronger evidence that speech has started.
                    "start_of_speech_sensitivity": (
                        types.StartSensitivity.START_SENSITIVITY_LOW
                    ),
                    "end_of_speech_sensitivity": (
                        types.EndSensitivity.END_SENSITIVITY_HIGH
                    ),
                    "prefix_padding_ms": 200,
                    "silence_duration_ms": 500,
                },
            },
            # Native audio consumes context quickly. Sliding-window compression
            # prevents an extended voice conversation from exhausting the
            # session context while retaining recent conversational history.
            "context_window_compression": types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
            ),
            "session_resumption": types.SessionResumptionConfig(
                handle=resumption_handle,
            ),
            "tools": [{
                "function_declarations": [{
                    "name": "set_facial_expression",
                    "description": (
                        "Changes Ada's visible facial expression without interrupting speech. "
                        "Select the expression matching the tone of Ada's current response."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "expression": {
                                "type": "string",
                                "enum": list(EXPRESSION_NAMES),
                                "description": "The facial expression Ada should display.",
                            }
                        },
                        "required": ["expression"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "get_home_state",
                    "description": (
                        "Returns current read-only Home Assistant home-plug state, "
                        "whether the user is home, recent local home occupancy, and any "
                        "active five-minute or latched habit condition. Use this when asked "
                        "about home plugs, occupancy, or whether plugs were left on."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }, {
                    "name": "control_entity",
                    "description": (
                        "Turn on or off a Home Assistant light, switch, fan, or input_boolean. "
                        "Use this when the user asks to turn something on or off."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "entity_id": {
                                "type": "string",
                                "description": "The Home Assistant entity_id to control, e.g. light.living_room.",
                            },
                            "on": {
                                "type": "boolean",
                                "description": "True to turn the entity on, false to turn it off.",
                            },
                        },
                        "required": ["entity_id", "on"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "list_home_devices",
                    "description": (
                        "Lists every controllable Home Assistant device (lights, switches, fans, input_booleans) "
                        "with its entity_id, friendly name, current state, domain, and availability. "
                        "Use this to answer 'what devices are available', 'what can I control', or to find "
                        "the exact entity_id before calling control_entity."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }, {
                    "name": "search_home_devices",
                    "description": (
                        "Searches controllable Home Assistant devices by name or entity_id. "
                        "Returns the best matching devices with entity_id, friendly name, state, domain, and availability. "
                        "Use this when the user asks to control a device by name (e.g. 'turn on kitchen table') "
                        "and you need to find the exact entity_id."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "A device name or keyword to search, e.g. 'kitchen table', 'front gate', or 'outlet'.",
                            }
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "list_sensors",
                    "description": (
                        "Lists available Home Assistant sensor entities with their current state, unit, and friendly name. "
                        "Use this when the user asks 'what sensors do we have', 'what can we monitor', or about environmental/power/energy information."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }, {
                    "name": "search_sensors",
                    "description": (
                        "Searches Home Assistant sensor entities by name or entity_id. "
                        "Returns matching sensors with current state, unit, and friendly name. "
                        "Use this when the user asks about a specific reading like 'what is the pool temperature' or 'what is the pool energy'."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "A sensor name or keyword to search, e.g. 'pool', 'temperature', 'pv power'.",
                            }
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "control_cover",
                    "description": (
                        "Open, close, or stop a Home Assistant cover such as a gate or roller shutter. "
                        "Use this when the user asks to open/close the gate, garage, or shutter. "
                        "Only call this tool after the user has explicitly confirmed there is nothing blocking the gate or shutter."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "entity_id": {
                                "type": "string",
                                "description": "The cover.* entity_id, e.g. cover.gate_motor.",
                            },
                            "action": {
                                "type": "string",
                                "enum": ["open", "close", "stop"],
                                "description": "The cover action: open, close, or stop.",
                            },
                        },
                        "required": ["entity_id", "action"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "press_button",
                    "description": (
                        "Press a Home Assistant button entity. "
                        "Use this for 'my position' buttons or one-shot commands."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "entity_id": {
                                "type": "string",
                                "description": "The button.* entity_id, e.g. button.gate_motor_my_position.",
                            },
                        },
                        "required": ["entity_id"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "control_media_player",
                    "description": (
                        "Control a Home Assistant media player (TV, speaker). "
                        "Supports turn_on, turn_off, media_play, media_pause, media_stop, "
                        "volume_up, volume_down, volume_mute, and select_source. "
                        "Use this to turn the TV on or off or change playback."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "entity_id": {
                                "type": "string",
                                "description": "The media_player.* entity_id, e.g. media_player.lg_webos_tv_nano81tsa.",
                            },
                            "action": {
                                "type": "string",
                                "enum": ["turn_on", "turn_off", "media_play", "media_pause", "media_stop", "volume_up", "volume_down", "volume_mute", "select_source"],
                                "description": "The media_player action.",
                            },
                            "source": {
                                "type": "string",
                                "description": "Required for select_source; the input/source name to select.",
                            },
                        },
                        "required": ["entity_id", "action"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "tv_action",
                    "description": (
                        "Send a command to the LG TV through the Home Assistant rest_command.tv_action service. "
                        "Use this for casting or navigation commands such as cmd='nav' with text='screenlive:workspace:1' or 'tony-omen:workspace:1'. "
                        "The cmd and text values are passed straight to the TV action REST command."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "cmd": {
                                "type": "string",
                                "description": "Command key, e.g. 'nav' or 'power'.",
                            },
                            "text": {
                                "type": "string",
                                "description": "Command text/payload, e.g. 'screenlive:workspace:1' or 'tony-omen:workspace:1'.",
                            },
                        },
                        "required": ["cmd"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "get_battery_status",
                    "description": (
                        "Returns current battery details: total and per-battery SOC, voltage, current, power, temperature, and state of health. "
                        "Use this when the user asks about battery levels, battery health, or battery status."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }, {
                    "name": "get_battery_detail",
                    "description": (
                        "Returns detailed readings for a single battery (1, 2, or 3). "
                        "Use this when the user asks for 'battery 1 details', 'battery 2 status', or 'tell me about each battery'."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "battery_index": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 3,
                                "description": "The battery number to detail: 1, 2, or 3.",
                            }
                        },
                        "required": ["battery_index"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "get_inverter_status",
                    "description": (
                        "Returns current inverter details: PV power, load power, grid power, battery power, AC output, voltage, frequency, and operating mode. "
                        "Use this when the user asks about the inverter, solar, grid, or load status."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }, {
                    "name": "get_pool_status",
                    "description": (
                        "Returns the current pool sensor and switch states, separating available and unavailable/unknown entities. "
                        "Use this when the user asks about the pool, pool pump, or pool energy."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }, {
                    "name": "get_rk600_weather",
                    "description": (
                        "Returns the local RK600 weather station readings: wind speed, wind direction, temperature, humidity, pressure, rainfall, and device status. "
                        "Use this when the user asks about the weather station, wind, or the RK600 card. "
                        "Do NOT use this for forecast/Met.no weather; use search_sensors('weather') for that."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }, {
                    "name": "get_dashboard_tab",
                    "description": (
                        "Reads the named tab from the michael-ha tony-test dashboard and returns "
                        "the entities it displays with their current states. "
                        "Use this when the user asks 'what is on the TPL tab', 'what devices are on V1', "
                        "or 'what does the TPL tab show'."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "tab": {
                                "type": "string",
                                "description": "The dashboard tab title, e.g. 'TPL', 'V0', 'V1', 'SK'.",
                            }
                        },
                        "required": ["tab"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "get_power_summary",
                    "description": (
                        "Returns the current G3 power summary for solar, grid, load, battery, and inverter. "
                        "Also returns min/max/mean over the requested hours. "
                        "Use this when the user asks about solar generation, grid usage, battery, load, or a power summary."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "hours": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 168,
                                "description": "How many hours of history to include in the summary. Defaults to 24.",
                            }
                        },
                        "additionalProperties": False,
                    },
                }, {
                    "name": "get_sensor_history",
                    "description": (
                        "Fetches the history of a single Home Assistant sensor for the requested hours. "
                        "Use this when the user asks about a specific sensor's behavior over time."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "entity_id": {
                                "type": "string",
                                "description": "The exact Home Assistant entity_id, e.g. sensor.inverters_1_pv_power.",
                            },
                            "hours": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 168,
                                "description": "How many hours of history to include. Defaults to 24.",
                            }
                        },
                        "required": ["entity_id"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "report_habit_observation",
                    "description": (
                        "Reports the structured result of a water or junk-food camera "
                        "observation requested by the ADA backend. Call this only when a "
                        "backend prompt supplies a challenge_id, after reviewing the full "
                        "observation window. Mere containers or food presence are not consumption."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "challenge_id": {"type": "string"},
                            "habit_key": {"type": "string", "enum": ["not_drinking_enough_water", "junk_food"]},
                            "observed": {"type": "boolean"},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "reason": {"type": "string"},
                            "item_identified": {"type": "string", "description": "For junk-food checks, the specific visible food or drink; empty when none is identifiable."},
                            "consumption_visible": {"type": "boolean", "description": "For junk-food checks, true only when actual eating or drinking is visibly confirmed."},
                            "classified_unhealthy": {"type": "boolean", "description": "For junk-food checks, true only when the identified item clearly belongs to the configured unhealthy categories."},
                        },
                        "required": ["challenge_id", "habit_key", "observed", "confidence", "reason"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "get_habit_status",
                    "description": (
                        "Returns every habit Ada tracks, including habits with no occurrences, "
                        "their lifecycle status, rolling seven-day occurrences and days, and "
                        "current monitor state and progress. Use this whenever the user asks "
                        "what habits are tracked or how their habits are progressing."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                }, {
                    "name": "ada_ha_get_state",
                    "description": (
                        "Returns the preloaded memory snapshot of the configured Home Assistant: person, home plugs, "
                        "and counts of controllable devices and sensors. Use this when the user "
                        "asks about the stored home state or what is in memory."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                }, {
                    "name": "ada_ha_search_devices",
                    "description": (
                        "Search the stored Home Assistant memory for controllable devices by name or entity_id. "
                        "Use this when the user asks 'what devices do we have' or 'find the kitchen light'."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "A device name or keyword, e.g. 'kitchen table'.",
                            }
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_ha_search_sensors",
                    "description": (
                        "Search the stored Home Assistant memory for sensor entities by name or entity_id. "
                        "Use this when the user asks 'what sensors do we have about power' or 'find the pool temperature sensor'."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "A sensor name or keyword, e.g. 'pool temperature'.",
                            }
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_ha_recall",
                    "description": (
                        "Free-form recall across the stored Home Assistant memory for devices and sensors. "
                        "Use this for broad questions like 'what sensors are about power' or 'pool devices'."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "The question or keyword to recall.",
                            }
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_ha_history",
                    "description": (
                        "Returns recent persisted snapshots of the Home Assistant state from memory. "
                        "Use this when the user asks what changed, what the state was earlier, or for a history of the home."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "hours": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 168,
                                "description": "How many hours back to include. Defaults to 24.",
                            },
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 50,
                                "description": "Maximum snapshots to return. Defaults to 10.",
                            }
                        },
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_ha_get_device_confidence",
                    "description": (
                        "Returns controllable devices grouped by user confidence: trusted_working, "
                        "trusted_broken, learning, or needs_integration. "
                        "Each device also includes a safety level: safe, caution, or dangerous. "
                        "Use this when the user asks what is broken, what needs setup, what is new, "
                        "what is trusted, or what is dangerous."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                }, {
                    "name": "ada_ha_set_device_confidence",
                    "description": (
                        "Set a controllable device's confidence and/or safety status. "
                        "Use this when the user says a device is broken, new, trusted, dangerous, safe, or needs caution."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "entity_id": {
                                "type": "string",
                                "description": "The exact Home Assistant entity_id.",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["trusted_working", "trusted_broken", "learning", "needs_integration"],
                                "description": "The confidence level to assign.",
                            },
                            "safety": {
                                "type": "string",
                                "enum": ["safe", "caution", "dangerous"],
                                "description": "The safety level to assign. Use this to mark devices that are dangerous or safe.",
                            }
                        },
                        "required": ["entity_id", "status"],
                        "additionalProperties": False,
                    },
                }, {
                    "name": "ada_session_recall",
                    "description": (
                        "Ask NotebookLM about previous voice sessions. "
                        "Use this when the user asks 'what did we talk about', 'do you remember', "
                        "or wants to recall something from an earlier conversation. "
                        "The recall runs in the background; the result will be spoken when ready."
                    ),
                    "behavior": types.Behavior.NON_BLOCKING,
                    "parameters_json_schema": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "The recall question, e.g. 'what did we discuss in the previous session?'.",
                            }
                        },
                        "required": ["question"],
                        "additionalProperties": False,
                    },
                }]
            }],
        }
        self._session_context = self._client.aio.live.connect(
            model=self.model,
            config=config,
        )
        self._session = await self._session_context.__aenter__()
        logger.info(
            "session=%s Gemini Live session connected (model=%s, voice=%s, video=%s)",
            self.session_id, self.model,
            self.voice,
            self.video_resolution,
        )

    async def send_audio(self, pcm16: bytes) -> None:
        if self._session is None:
            raise RuntimeError("provider is not connected")
        async with self._send_lock:
            await self._session.send_realtime_input(
                audio=types.Blob(data=pcm16, mime_type="audio/pcm;rate=16000")
            )

    async def send_video(self, jpeg: bytes) -> None:
        if self._session is None:
            raise RuntimeError("provider is not connected")
        async with self._send_lock:
            await self._session.send_realtime_input(
                video=types.Blob(data=jpeg, mime_type="image/jpeg")
            )

    async def send_text_turn(self, text: str) -> None:
        if self._session is None:
            raise RuntimeError("provider is not connected")
        async with self._send_lock:
            await self._session.send_client_content(
                turns=types.Content(
                    role="user",
                    parts=[types.Part.from_text(text=text)],
                ),
                turn_complete=True,
            )

    async def send_habit_alert(self, jpeg: bytes, alert: str) -> None:
        if self._session is None:
            raise RuntimeError("provider is not connected")
        async with self._send_lock:
            await self._session.send_client_content(
                turns=types.Content(role="user", parts=[
                    types.Part.from_text(text=alert),
                    types.Part.from_bytes(data=jpeg, mime_type="image/jpeg"),
                ]),
                turn_complete=True,
            )

    async def events(self) -> AsyncIterator[ProviderEvent]:
        if self._session is None:
            raise RuntimeError("provider is not connected")

        response_active = False
        input_transcript = ""
        assistant_turn_text = ""
        response_started_at = 0.0
        response_audio_chunks = 0
        response_audio_bytes = 0

        while not self._closed:
            async for message in self._session.receive():
                update = message.session_resumption_update
                if update and update.resumable and update.new_handle:
                    self.resumption_handle = update.new_handle
                if message.go_away:
                    self.go_away_time_left = str(message.go_away.time_left or "")
                    yield ProviderEvent("go_away", {"time_left": self.go_away_time_left})
                    return
                tool_call = message.tool_call
                if tool_call and tool_call.function_calls:
                    function_responses = []
                    for call in tool_call.function_calls:
                        logger.info(
                            "session=%s function_call received id=%s name=%s args=%r",
                            self.session_id, call.id, call.name, call.args,
                        )
                        requested = (call.args or {}).get("expression")
                        if call.name == "set_facial_expression" and requested in EXPRESSION_NAMES:
                            yield ProviderEvent("expression", {"name": requested})
                            result = {"output": f"Ada is now {requested}"}
                        elif call.name == "get_home_state" and self.home_assistant_client is not None:
                            try:
                                snapshot = await self.home_assistant_client.snapshot()
                                plugs_on_named = [
                                    {"entity_id": e, "name": snapshot.plug_names.get(e, e)}
                                    for e in snapshot.plugs_on
                                ]
                                all_plugs = {
                                    e: {"name": snapshot.plug_names.get(e, e), "state": state}
                                    for e, state in snapshot.plug_states.items()
                                }
                                result = {
                                    "output": {
                                        "person": snapshot.person_state,
                                        "plugs_on": plugs_on_named,
                                        "all_plugs": all_plugs,
                                    }
                                }
                            except Exception as exc:
                                result = {"error": f"home state failed: {exc}"}
                        elif call.name == "list_home_devices" and self.home_assistant_client is not None:
                            try:
                                devices = await self.home_assistant_client.entities()
                                result = {"output": devices}
                            except Exception as exc:
                                result = {"error": f"list_home_devices failed: {exc}"}
                        elif call.name == "search_home_devices" and self.home_assistant_client is not None:
                            try:
                                query = (call.args or {}).get("query", "")
                                devices = await self.home_assistant_client.search_entities(str(query))
                                result = {"output": devices}
                            except Exception as exc:
                                result = {"error": f"search_home_devices failed: {exc}"}
                        elif call.name == "control_entity" and self.home_assistant_client is not None:
                            try:
                                args = dict(call.args or {})
                                entity_id = args.get("entity_id")
                                on = args.get("on")
                                if not entity_id or not isinstance(on, bool):
                                    result = {"error": "entity_id and on are required"}
                                else:
                                    outcome = await self.home_assistant_client.set_power(entity_id, on)
                                    result = {"output": f"Turned {'on' if on else 'off'} {entity_id}: {outcome}"}
                            except Exception as exc:
                                result = {"error": f"control_entity failed: {exc}"}
                        elif call.name == "list_sensors" and self.home_assistant_client is not None:
                            try:
                                sensors = await self.home_assistant_client.sensors(limit=50)
                                result = {"output": sensors}
                            except Exception as exc:
                                result = {"error": f"list_sensors failed: {exc}"}
                        elif call.name == "search_sensors" and self.home_assistant_client is not None:
                            try:
                                args = dict(call.args or {})
                                query = args.get("query", "")
                                sensors = await self.home_assistant_client.sensors(search=str(query), limit=10)
                                result = {"output": sensors}
                            except Exception as exc:
                                result = {"error": f"search_sensors failed: {exc}"}
                        elif call.name == "control_cover" and self.home_assistant_client is not None:
                            try:
                                args = dict(call.args or {})
                                entity_id = args.get("entity_id")
                                action = args.get("action")
                                if not entity_id or not action:
                                    result = {"error": "entity_id and action are required"}
                                else:
                                    outcome = await self.home_assistant_client.control_cover(entity_id, action)
                                    result = {"output": outcome}
                            except Exception as exc:
                                result = {"error": f"control_cover failed: {exc}"}
                        elif call.name == "press_button" and self.home_assistant_client is not None:
                            try:
                                args = dict(call.args or {})
                                entity_id = args.get("entity_id")
                                if not entity_id:
                                    result = {"error": "entity_id is required"}
                                else:
                                    outcome = await self.home_assistant_client.press_button(entity_id)
                                    result = {"output": outcome}
                            except Exception as exc:
                                result = {"error": f"press_button failed: {exc}"}
                        elif call.name == "control_media_player" and self.home_assistant_client is not None:
                            try:
                                args = dict(call.args or {})
                                entity_id = args.get("entity_id")
                                action = args.get("action")
                                source = args.get("source")
                                if not entity_id or not action:
                                    result = {"error": "entity_id and action are required"}
                                else:
                                    outcome = await self.home_assistant_client.control_media_player(entity_id, action, source)
                                    result = {"output": outcome}
                            except Exception as exc:
                                result = {"error": f"control_media_player failed: {exc}"}
                        elif call.name == "tv_action" and self.home_assistant_client is not None:
                            try:
                                args = dict(call.args or {})
                                cmd = args.get("cmd")
                                text = args.get("text", "")
                                if not cmd:
                                    result = {"error": "cmd is required"}
                                else:
                                    outcome = await self.home_assistant_client.tv_action(cmd, text)
                                    result = {"output": outcome}
                            except Exception as exc:
                                result = {"error": f"tv_action failed: {exc}"}
                        elif call.name == "get_battery_status" and self.home_assistant_client is not None:
                            try:
                                status = await self.home_assistant_client.battery_status()
                                result = {"output": status}
                            except Exception as exc:
                                result = {"error": f"get_battery_status failed: {exc}"}
                        elif call.name == "get_battery_detail" and self.home_assistant_client is not None:
                            try:
                                args = dict(call.args or {})
                                index = int(args.get("battery_index", 1))
                                status = await self.home_assistant_client.battery_detail(index)
                                result = {"output": status}
                            except Exception as exc:
                                result = {"error": f"get_battery_detail failed: {exc}"}
                        elif call.name == "get_inverter_status" and self.home_assistant_client is not None:
                            try:
                                status = await self.home_assistant_client.inverter_status()
                                result = {"output": status}
                            except Exception as exc:
                                result = {"error": f"get_inverter_status failed: {exc}"}
                        elif call.name == "get_pool_status" and self.home_assistant_client is not None:
                            try:
                                status = await self.home_assistant_client.pool_status()
                                result = {"output": status}
                            except Exception as exc:
                                result = {"error": f"get_pool_status failed: {exc}"}
                        elif call.name == "get_rk600_weather" and self.home_assistant_client is not None:
                            try:
                                weather = await self.home_assistant_client.rk600_weather()
                                result = {"output": weather}
                            except Exception as exc:
                                result = {"error": f"get_rk600_weather failed: {exc}"}
                        elif call.name == "get_dashboard_tab" and self.home_assistant_client is not None:
                            try:
                                args = dict(call.args or {})
                                tab = args.get("tab")
                                if not tab:
                                    result = {"error": "tab is required"}
                                else:
                                    info = await self.home_assistant_client.dashboard_tab(str(tab))
                                    result = {"output": info}
                            except Exception as exc:
                                result = {"error": f"get_dashboard_tab failed: {exc}"}
                        elif call.name == "get_power_summary" and self.home_assistant_client is not None:
                            try:
                                args = dict(call.args or {})
                                hours = int(args.get("hours", 24))
                                summary = await self.home_assistant_client.power_summary(hours=hours)
                                result = {"output": summary}
                            except Exception as exc:
                                result = {"error": f"get_power_summary failed: {exc}"}
                        elif call.name == "get_sensor_history" and self.home_assistant_client is not None:
                            try:
                                args = dict(call.args or {})
                                entity_id = args.get("entity_id")
                                hours = int(args.get("hours", 24))
                                if not entity_id:
                                    result = {"error": "entity_id is required"}
                                else:
                                    history = await self.home_assistant_client.history(entity_id, hours=hours)
                                    result = {"output": history}
                            except Exception as exc:
                                result = {"error": f"get_sensor_history failed: {exc}"}
                        elif call.name == "report_habit_observation":
                            args = dict(call.args or {})
                            yield ProviderEvent("habit_observation", args)
                            result = {"output": "Observation delivered to the habit monitor"}
                        elif call.name == "get_habit_status" and self.habit_state_getter is not None:
                            result = {"output": self.habit_state_getter()}
                        elif call.name == "ada_session_recall":
                            question = (call.args or {}).get("question", "What did we discuss in the previous session?")
                            recall_status = self.conversation.start_recall(str(question), self._on_recall_complete)
                            result = {"output": recall_status}
                        else:
                            if self.tool_runner is not None:
                                try:
                                    output = await self.tool_runner.execute(str(call.name), dict(call.args or {}))
                                    result = {"output": output}
                                except Exception as exc:
                                    result = {"error": f"{call.name} failed: {exc}"}
                            else:
                                result = {"error": "Unsupported or unavailable function"}
                        function_responses.append(types.FunctionResponse(
                            id=call.id,
                            name=call.name or "set_facial_expression",
                            response=result,
                            # The expression tool often arrives before audio.
                            # WHEN_IDLE lets Gemini continue the spoken reply;
                            # SILENT would add the result to context without
                            # triggering generation, leaving Ada mute.
                            scheduling=types.FunctionResponseScheduling.WHEN_IDLE,
                        ))
                        logger.info(
                            "session=%s function_call result id=%s name=%s result=%r",
                            self.session_id, call.id, call.name, result,
                        )
                    await self._session.send_tool_response(
                        function_responses=function_responses
                    )
                    logger.info(
                        "session=%s function_call responses sent count=%d",
                        self.session_id, len(function_responses),
                    )

                content = message.server_content
                if content is None:
                    continue

                if content.interrupted:
                    elapsed = time.monotonic() - response_started_at if response_started_at else 0.0
                    logger.warning(
                        "session=%s assistant interrupted response_active=%s age_ms=%d audio_chunks=%d "
                        "audio_bytes=%d pending_input_transcript=%r",
                        self.session_id, response_active, elapsed * 1000, response_audio_chunks,
                        response_audio_bytes, input_transcript.strip(),
                    )
                    response_active = False
                    yield ProviderEvent("response_interrupted", {})
                    # Gemini 3.1 can include several content parts in one event.
                    # Any audio/transcript accompanying an interruption belongs
                    # to the cancelled response and must not restart playback.
                    continue

                transcription = content.input_transcription
                if transcription and transcription.text:
                    input_transcript += transcription.text

                output_transcription = content.output_transcription
                if output_transcription and output_transcription.text:
                    if not response_active:
                        response_active = True
                        response_started_at = time.monotonic()
                        response_audio_chunks = 0
                        response_audio_bytes = 0
                        yield ProviderEvent("response_started", {})
                    assistant_turn_text += output_transcription.text
                    yield ProviderEvent(
                        "assistant_transcript_delta",
                        {"text": output_transcription.text},
                    )

                model_turn = content.model_turn
                if model_turn:
                    for part in model_turn.parts or []:
                        inline_data = part.inline_data
                        if inline_data and inline_data.data:
                            if not response_active:
                                response_active = True
                                response_started_at = time.monotonic()
                                response_audio_chunks = 0
                                response_audio_bytes = 0
                                yield ProviderEvent("response_started", {})
                            response_audio_chunks += 1
                            response_audio_bytes += len(inline_data.data)
                            yield ProviderEvent("audio", {"pcm16": inline_data.data})

                if content.turn_complete:
                    transcript = input_transcript.strip()
                    if transcript:
                        self.conversation.add_user(transcript)
                        yield ProviderEvent("user_transcript", {"text": transcript})
                    input_transcript = ""
                    if assistant_turn_text.strip():
                        self.conversation.add_assistant(assistant_turn_text)
                        assistant_turn_text = ""
                    if response_active:
                        elapsed = time.monotonic() - response_started_at if response_started_at else 0.0
                        logger.info(
                            "session=%s assistant response completed duration_ms=%d audio_chunks=%d audio_bytes=%d",
                            self.session_id, elapsed * 1000, response_audio_chunks, response_audio_bytes,
                        )
                        yield ProviderEvent("response_completed", {})
                    response_active = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Persist conversation to NotebookLM in the background so we don't block disconnect.
        if self.conversation:
            _ = asyncio.create_task(self.conversation.persist())
        if self._session_context is not None:
            try:
                await asyncio.wait_for(
                    self._session_context.__aexit__(None, None, None), timeout=3
                )
            except (TimeoutError, Exception):
                logger.debug("Gemini session close did not complete cleanly", exc_info=True)
        if self._client is not None:
            try:
                await self._client.aio.aclose()
            except Exception:
                logger.debug("Gemini client close did not complete cleanly", exc_info=True)


def create_provider(instructions: str | None = None, tool_runner: Any = None,
                    home_assistant_client: Any = None, habit_state_getter: Any = None,
                    session_id: str | None = None) -> RealtimeProvider:
    return GeminiLiveProvider(
        instructions=instructions,
        tool_runner=tool_runner,
        home_assistant_client=home_assistant_client,
        habit_state_getter=habit_state_getter,
        session_id=session_id,
    )

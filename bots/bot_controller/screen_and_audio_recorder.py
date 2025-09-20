import logging
import os
import subprocess
import time
import tempfile
import re

logger = logging.getLogger(__name__)


class ScreenAndAudioRecorder:
    """
    Records screen and audio from web meetings using FFmpeg and PulseAudio.
    
    CRITICAL TIMING: For reliable audio capture, set up audio BEFORE Chrome starts:
    
    # CORRECT ORDER:
    recorder = ScreenAndAudioRecorder(...)
    recorder.setup_audio_before_chrome()  # Do this FIRST!
    # THEN start Chrome/WebDriver and join meeting
    recorder.start_recording(display_var)
    
    # FALLBACK (if Chrome already running):
    recorder.ensure_chrome_audio_capture()  # Move Chrome's audio to ChromeSink
    recorder.start_recording(display_var)
    
    This ensures Chrome's audio is properly routed to ChromeSink for capture.
    """
    def __init__(self, file_location, recording_dimensions, audio_only):
        self.file_location = file_location
        self.ffmpeg_proc = None
        # Screen will have buffer, we will crop to the recording dimensions
        self.screen_dimensions = (recording_dimensions[0] + 10, recording_dimensions[1] + 10)
        self.recording_dimensions = recording_dimensions
        self.audio_only = audio_only
        self.paused = False
        self.xterm_proc = None
        self.ffmpeg_log_file = None
        self.recording_started_time = None
        self.pulseaudio_setup_attempted = False
        # New optimization attributes
        self.ffmpeg_thread_count = 2
        self.audio_resampler = 'speex-float-3'

    def __del__(self):
        """Ensure ffmpeg process is cleaned up on object destruction"""
        try:
            if self.ffmpeg_proc and self.ffmpeg_proc.poll() is None:
                logger.warning("FFmpeg process still running during cleanup, terminating")
                self.stop_recording()
        except:
            pass  # Ignore errors during cleanup

    def _get_audio_input_options(self):
        """
        Detect available audio input options and return appropriate FFmpeg parameters.
        Returns None if no audio input is available.
        """
        # Hard disable via env
        if os.environ.get('DISABLE_AUDIO_RECORDING', '').lower() in ('1', 'true', 'yes'):
            logger.info("Audio recording disabled by environment variable DISABLE_AUDIO_RECORDING")
            return None

        force_pulse = os.environ.get('FORCE_PULSE', '').lower() in ('1', 'true', 'yes')

        # Always ensure PulseAudio is running if we plan to use it
        if force_pulse or not self.pulseaudio_setup_attempted:
            self.pulseaudio_setup_attempted = True
            if self.start_pulseaudio_if_needed():
                # Try to create ChromeSink if missing
                self.setup_pulseaudio_for_meeting_capture()

        # If FORCE_PULSE is set, prefer ChromeSink but only if it actually exists
        if force_pulse:
            if self._wait_for_chromesink_monitor(timeout_sec=5):
                logger.info("Using PulseAudio ChromeSink.monitor for meeting audio")
                # Hardened FFmpeg audio capture with explicit format to prevent white noise
                return [
                    "-thread_queue_size", "4096", 
                    "-f", "pulse", 
                    "-channels", "2", 
                    "-sample_rate", "48000", 
                    "-sample_fmt", "s16",  # Explicit s16 to avoid float32→s16 noise
                    "-guess_layout_max", "0",
                    "-i", "ChromeSink.monitor"
                ]
            else:
                logger.warning("FORCE_PULSE requested but ChromeSink.monitor not found; falling back to Pulse default")
                # fall through to default detection

        # If ChromeSink exists (from earlier setup), use it with hardened parameters
        if self._check_pulseaudio_chromesink_exists() and self._wait_for_chromesink_monitor(timeout_sec=2):
            logger.info("Found existing ChromeSink.monitor, using it for meeting audio")
            return [
                "-thread_queue_size", "4096", 
                "-f", "pulse", 
                "-channels", "2", 
                "-sample_rate", "48000", 
                "-sample_fmt", "s16",  # Explicit s16 to prevent noise
                "-guess_layout_max", "0",
                "-i", "ChromeSink.monitor"
            ]

        # Fallbacks with safer parameters
        logger.warning("PulseAudio ChromeSink not available, falling back to traditional audio sources")
        audio_methods = [
            (["-thread_queue_size", "4096", "-f", "pulse", "-ac", "2", "-ar", "48000", "-sample_fmt", "s16", "-i", "default"], "PulseAudio default"),
            (["-thread_queue_size", "4096", "-f", "alsa",  "-ac", "2", "-ar", "48000", "-i", "default"], "ALSA default"),
            (["-thread_queue_size", "4096", "-f", "alsa",  "-ac", "2", "-ar", "48000", "-i", "hw:0"],    "ALSA hw:0"),
        ]
        for audio_cmd, description in audio_methods:
            if self._test_audio_input(audio_cmd, description):
                logger.info(f"Using fallback audio input method: {description}")
                return audio_cmd

        logger.warning("No working audio input found, will record video only")
        return None

    def _check_pulseaudio_chromesink_exists(self):
        """Check if ChromeSink already exists in PulseAudio"""
        try:
            result = subprocess.run(
                ["pactl", "list", "short", "sinks"], 
                capture_output=True, 
                timeout=5, 
                text=True
            )
            if result.returncode == 0:
                # Check if ChromeSink is in the output
                return "ChromeSink" in result.stdout
        except:
            pass
        return False

    def export_pulse_env_for_children(self):
        """
        Set comprehensive PulseAudio environment variables for Chrome process.
        Call this BEFORE starting any audio processes to ensure proper routing.
        """
        try:
            uid = os.getuid()
            xdg_runtime_dir = f"/run/user/{uid}"
            pulse_runtime_dir = f"{xdg_runtime_dir}/pulse"
            pulse_server = f"unix:{pulse_runtime_dir}/native"
            
            # Create runtime directories if they don't exist
            os.makedirs(xdg_runtime_dir, exist_ok=True)
            os.makedirs(pulse_runtime_dir, exist_ok=True)
            
            # Set comprehensive environment variables for child processes
            # These match Google Meet's audio format and reduce jitter
            env_vars = {
                'XDG_RUNTIME_DIR': xdg_runtime_dir,
                'PULSE_RUNTIME_PATH': pulse_runtime_dir,
                'PULSE_RUNTIME_DIR': pulse_runtime_dir, 
                'PULSE_SERVER': pulse_server,
                'PULSE_SINK': 'ChromeSink',  # Force new clients to use ChromeSink
                'PULSE_LATENCY_MSEC': '60',  # Low latency for real-time audio
            }
            
            for key, value in env_vars.items():
                os.environ[key] = value
                logger.info(f"Set {key}={value}")
            
            return True
            
        except Exception as e:
            logger.warning(f"Failed to set PulseAudio environment: {e}")
            return False

    def ensure_sink_and_monitor_exist(self, sink_name="ChromeSink", tries=10, sleep_s=0.2):
        """
        Wait for PulseAudio sink and monitor to be created (async process).
        Returns True if both sink and monitor exist, False otherwise.
        """
        logger.info(f"Waiting for {sink_name} and {sink_name}.monitor to be available...")
        
        for attempt in range(tries):
            try:
                # Check for sink
                sink_result = subprocess.run(
                    ["pactl", "list", "short", "sinks"], 
                    capture_output=True, text=True, timeout=5
                )
                
                # Check for monitor source
                source_result = subprocess.run(
                    ["pactl", "list", "short", "sources"], 
                    capture_output=True, text=True, timeout=5
                )
                
                if (sink_result.returncode == 0 and source_result.returncode == 0):
                    sink_exists = sink_name in sink_result.stdout
                    monitor_exists = f"{sink_name}.monitor" in source_result.stdout
                    
                    if sink_exists and monitor_exists:
                        logger.info(f"Both {sink_name} and {sink_name}.monitor are available")
                        return True
                    else:
                        logger.debug(f"Attempt {attempt + 1}/{tries}: sink={sink_exists}, monitor={monitor_exists}")
                
            except Exception as e:
                logger.debug(f"Attempt {attempt + 1}/{tries} failed: {e}")
            
            time.sleep(sleep_s)
        
        # If we get here, log diagnostic info
        logger.error(f"Failed to find {sink_name} after {tries} attempts")
        try:
            info_result = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=5)
            logger.error(f"PulseAudio info: {info_result.stdout}")
            
            sinks_result = subprocess.run(["pactl", "list", "sinks"], capture_output=True, text=True, timeout=5)
            logger.error(f"Available sinks: {sinks_result.stdout}")
        except:
            pass
            
        return False

    def _wait_for_chromesink_monitor(self, timeout_sec=5):
        """Poll PulseAudio for ChromeSink.monitor to exist."""
        end = time.time() + timeout_sec
        while time.time() < end:
            try:
                result = subprocess.run(["pactl", "list", "short", "sources"],
                                        capture_output=True, timeout=3, text=True)
                if result.returncode == 0 and "ChromeSink.monitor" in result.stdout:
                    return True
            except Exception:
                pass
            time.sleep(0.25)
        return False

    def move_chrome_audio_to_chromesink(self, poll_iterations=20, poll_delay=0.5):
        """
        Move any active Chrome sink inputs to ChromeSink for audio capture.
        Polls for Chrome inputs to appear (needed if Chrome starts after this call).
        """
        try:
            logger.info(f"Attempting to move Chrome audio streams to ChromeSink (polling {poll_iterations} times)")
            
            moved_count = 0
            for attempt in range(poll_iterations):
                # Get current sink inputs
                short_result = subprocess.run(["pactl", "list", "short", "sink-inputs"], 
                                            capture_output=True, text=True, timeout=5)
                
                if short_result.returncode != 0:
                    logger.debug(f"Attempt {attempt + 1}: Failed to list sink inputs")
                    time.sleep(poll_delay)
                    continue
                    
                if not short_result.stdout.strip():
                    logger.debug(f"Attempt {attempt + 1}: No active sink inputs found")
                    time.sleep(poll_delay)
                    continue
                
                # Look for Chrome in the short list first (faster)
                chrome_found_in_short = False
                for line in short_result.stdout.strip().split('\n'):
                    if line.strip() and any(chrome_word in line.lower() for chrome_word in ['chrome', 'chromium']):
                        chrome_found_in_short = True
                        break
                
                if not chrome_found_in_short:
                    logger.debug(f"Attempt {attempt + 1}: No Chrome sink inputs in short list")
                    time.sleep(poll_delay)
                    continue
                
                # Get detailed sink inputs with properties
                result = subprocess.run(["pactl", "list", "sink-inputs"], 
                                      capture_output=True, text=True, timeout=5)
                if result.returncode != 0:
                    logger.debug(f"Attempt {attempt + 1}: Failed to get detailed sink input list")
                    time.sleep(poll_delay)
                    continue
                
                # Split by sink input blocks and process each
                blocks = re.split(r"\n(?=Sink Input #\d+)", result.stdout)
                attempt_moved = 0
                
                for block in blocks:
                    # Extract sink input ID
                    id_match = re.search(r"Sink Input #(\d+)", block)
                    if not id_match:
                        continue
                        
                    sink_input_id = id_match.group(1)
                    
                    # Enhanced Chrome detection patterns
                    chrome_indicators = [
                        'application.name = "Google Chrome"',
                        'application.name = "Chromium"',
                        'application.name = "Chrome"',
                        'application.name = "chrome"',
                        'application.name = "chromium"',
                        'application.process.binary = "chrome"',
                        'application.process.binary = "chromium"',
                        'application.process.binary = "google-chrome"',
                        'media.role = "phone"',  # WebRTC audio often has this role
                    ]
                    
                    if any(indicator in block for indicator in chrome_indicators):
                        logger.info(f"Found Chrome sink input #{sink_input_id}, moving to ChromeSink")
                        
                        # Log some details about this sink input
                        for line in block.split('\n')[:10]:
                            if any(key in line for key in ['application.name', 'application.process.binary', 'media.role']):
                                logger.info(f"  {line.strip()}")
                        
                        move_result = subprocess.run(
                            ["pactl", "move-sink-input", sink_input_id, "ChromeSink"], 
                            capture_output=True, timeout=5
                        )
                        
                        if move_result.returncode == 0:
                            attempt_moved += 1
                            moved_count += 1
                            logger.info(f"✓ Successfully moved Chrome sink input #{sink_input_id} to ChromeSink")
                        else:
                            logger.warning(f"✗ Failed to move sink input #{sink_input_id}: {move_result.stderr.decode()}")
                
                if attempt_moved > 0:
                    logger.info(f"Moved {attempt_moved} Chrome streams in attempt {attempt + 1}")
                    # Give a moment for audio to start flowing, then check if we have more
                    time.sleep(0.5)
                    
                    # If we moved something, continue polling a bit more to catch any new streams
                    continue
                
                # No Chrome streams found this attempt
                logger.debug(f"Attempt {attempt + 1}: No Chrome audio streams found to move")
                time.sleep(poll_delay)
            
            if moved_count > 0:
                logger.info(f"✓ Total: Moved {moved_count} Chrome audio streams to ChromeSink")
                return True
            else:
                logger.warning("✗ No Chrome audio streams found after all polling attempts")
                logger.info("This usually means:")
                logger.info("  1. Chrome was started with audio disabled (--mute-audio)")
                logger.info("  2. Chrome hasn't connected to PulseAudio yet")
                logger.info("  3. Meet tab is muted or no remote audio to play")
                logger.info("  4. Chrome is using a different audio backend (ALSA direct)")
                return False
                
        except Exception as e:
            logger.warning(f"Failed to move Chrome audio to ChromeSink: {e}")
            return False

    def monitor_has_signal(self, timeout_sec=3):
        """Check if ChromeSink.monitor has actual audio signal (not just silence)."""
        try:
            logger.debug("Probing ChromeSink.monitor for audio signal")
            
            # Quick ffmpeg probe with volumedetect
            cmd = ["ffmpeg", "-hide_banner", "-nostats", "-y", 
                   "-f", "pulse", "-i", "ChromeSink.monitor",
                   "-t", "1.5", "-vn", "-af", "volumedetect", 
                   "-f", "null", "-"]
            
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, 
                                  text=True, timeout=timeout_sec+2)
            
            # Check for volume detection in stderr
            if "max_volume:" in result.stderr:
                # Check for silence indicators
                silence_patterns = [
                    "max_volume: 0.0 dB",
                    "max_volume: -inf dB", 
                    "max_volume: -120.0 dB"  # Very quiet
                ]
                
                if any(pattern in result.stderr for pattern in silence_patterns):
                    logger.debug("ChromeSink.monitor appears to be silent")
                    return False
                else:
                    # Extract the actual volume value for logging
                    volume_match = re.search(r"max_volume:\s*([-\d.]+)\s*dB", result.stderr)
                    if volume_match:
                        volume = volume_match.group(1)
                        logger.debug(f"ChromeSink.monitor has audio signal (max_volume: {volume} dB)")
                    else:
                        logger.debug("ChromeSink.monitor has audio signal")
                    return True
            else:
                logger.debug("Could not detect volume levels, assuming no signal")
                return False
                
        except subprocess.TimeoutExpired:
            logger.warning("Audio signal detection timed out")
            return False
        except Exception as e:
            logger.warning(f"Failed to check audio signal: {e}")
            return False

    def setup_audio_before_chrome(self):
        """
        Set up PulseAudio and ChromeSink before Chrome starts.
        This is the most reliable way to ensure Chrome uses ChromeSink.
        Call this before initializing webdriver or navigating to Meet.
        
        CRITICAL: This must be called BEFORE Chrome starts for best results!
        """
        logger.info("=== Setting up bulletproof audio BEFORE Chrome startup ===")
        
        # STEP 1: Set environment variables FIRST (essential for child processes)
        logger.info("Step 1: Setting PulseAudio environment variables")
        if not self.export_pulse_env_for_children():
            logger.error("✗ Could not set PulseAudio environment")
            return False
        
        # STEP 2: Start PulseAudio if needed
        logger.info("Step 2: Starting PulseAudio daemon")
        if not self.start_pulseaudio_if_needed():
            logger.error("✗ Could not start PulseAudio")
            return False
            
        # STEP 3: Create bulletproof ChromeSink (null sink with 48k stereo)
        logger.info("Step 3: Creating bulletproof ChromeSink")
        if not self.setup_pulseaudio_for_meeting_capture():
            logger.error("✗ Could not set up ChromeSink")
            return False
            
        # STEP 4: Final verification
        logger.info("Step 4: Verifying ChromeSink setup")
        if self.ensure_sink_and_monitor_exist():
            logger.info("✓ PulseAudio setup complete - Chrome should use ChromeSink")
            # Quick diagnostic
            if self.quick_audio_diagnostics():
                logger.info("✓ Audio system is ready for Chrome")
                return True
            else:
                logger.warning("⚠ Audio system has issues but may still work")
                return True  # Don't fail completely, might still work
        else:
            logger.error("✗ ChromeSink.monitor not available after setup")
            return False

    def ensure_chrome_audio_capture(self, retry_count=3):
        """
        Ensure Chrome's audio is being captured by ChromeSink.
        Call this after meeting starts or when you detect audio activity.
        This uses the new polling-based Chrome input mover.
        """
        logger.info("=== Ensuring Chrome audio is routed to ChromeSink ===")
        
        for attempt in range(retry_count):
            logger.info(f"Attempt {attempt + 1}/{retry_count}: Checking/moving Chrome audio")
            
            # Check if we already have audio signal
            if self.monitor_has_signal(timeout_sec=2):
                logger.info("✓ ChromeSink.monitor already has audio signal")
                return True
                
            # Try to move Chrome's audio streams (with built-in polling)
            if self.move_chrome_audio_to_chromesink(poll_iterations=20, poll_delay=0.5):
                # Wait a moment for audio to start flowing
                time.sleep(1)
                
                # Check if we now have signal
                if self.monitor_has_signal(timeout_sec=2):
                    logger.info("✓ Successfully routed Chrome audio to ChromeSink")
                    return True
                else:
                    logger.warning(f"Moved Chrome streams but still no audio signal (attempt {attempt + 1})")
            else:
                logger.warning(f"Could not find/move Chrome audio streams (attempt {attempt + 1})")
            
            # Wait before next attempt
            if attempt < retry_count - 1:
                logger.info("Waiting before next attempt...")
                time.sleep(2)
        
        logger.error("✗ Could not ensure Chrome audio is routed to ChromeSink after all attempts")
        # Run diagnostics to help user understand what went wrong
        self.diagnose_chrome_audio_routing()
        return False

    def fallback_move_chrome_audio(self):
        """
        Simple fallback method to move Chrome audio to ChromeSink.
        Call this once you detect meeting has started (e.g., first caption appears).
        Returns True if audio is now flowing to ChromeSink.
        """
        logger.info("=== Attempting fallback Chrome audio routing ===")
        
        # First check if we already have signal
        if self.monitor_has_signal(timeout_sec=1):
            logger.info("✓ ChromeSink already has audio signal, no move needed")
            return True
        
        # Try to move Chrome audio streams with shorter polling
        if self.move_chrome_audio_to_chromesink(poll_iterations=10, poll_delay=0.3):
            # Wait a moment and check again
            time.sleep(0.5)
            if self.monitor_has_signal(timeout_sec=2):
                logger.info("✓ Successfully routed Chrome audio via fallback method")
                return True
        
        logger.warning("✗ Fallback Chrome audio routing failed")
        return False

    def diagnose_chrome_audio_routing(self):
        """
        Comprehensive diagnostics for Chrome audio routing issues.
        Call this when Chrome audio streams are not found.
        """
        logger.info("=== Chrome Audio Routing Diagnostics ===")
        
        try:
            # 1. Check PulseAudio status
            result = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                logger.info("✓ PulseAudio is running")
            else:
                logger.error("✗ PulseAudio is not running")
                return
            
            # 2. Check ChromeSink exists
            sinks = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True, timeout=5)
            if sinks.returncode == 0:
                chromesink_found = "ChromeSink" in sinks.stdout
                logger.info(f"{'✓' if chromesink_found else '✗'} ChromeSink exists: {chromesink_found}")
                if chromesink_found:
                    for line in sinks.stdout.strip().split('\n'):
                        if 'ChromeSink' in line:
                            logger.info(f"  ChromeSink: {line}")
                else:
                    logger.info("Available sinks:")
                    for line in sinks.stdout.strip().split('\n'):
                        logger.info(f"  {line}")
            
            # 3. Check ChromeSink.monitor exists
            sources = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=5)
            if sources.returncode == 0:
                monitor_found = "ChromeSink.monitor" in sources.stdout
                logger.info(f"{'✓' if monitor_found else '✗'} ChromeSink.monitor exists: {monitor_found}")
                if monitor_found:
                    for line in sources.stdout.strip().split('\n'):
                        if 'ChromeSink.monitor' in line:
                            logger.info(f"  Monitor: {line}")
            
            # 4. Check active sink inputs (Chrome streams) with JSON format for better parsing
            logger.info("Checking Chrome sink inputs with detailed JSON output...")
            sink_inputs = subprocess.run(["pactl", "list", "short", "sink-inputs"], capture_output=True, text=True, timeout=5)
            if sink_inputs.returncode == 0:
                chrome_streams = []
                all_streams = []
                for line in sink_inputs.stdout.strip().split('\n'):
                    if line.strip():
                        all_streams.append(line)
                        if any(chrome_word in line.lower() for chrome_word in ['chrome', 'chromium']):
                            chrome_streams.append(line)
                
                logger.info(f"{'✓' if chrome_streams else '✗'} Chrome sink inputs found: {len(chrome_streams)}")
                if chrome_streams:
                    for stream in chrome_streams:
                        logger.info(f"  Chrome stream: {stream}")
                else:
                    logger.info(f"All active sink inputs ({len(all_streams)}):")
                    for stream in all_streams[:10]:  # Limit to first 10
                        logger.info(f"  {stream}")
                
                # Try JSON format for more detailed Chrome detection (newer pactl versions)
                try:
                    json_result = subprocess.run(["pactl", "-f", "json", "list", "sink-inputs"], 
                                               capture_output=True, text=True, timeout=5)
                    if json_result.returncode == 0:
                        import json
                        try:
                            sink_data = json.loads(json_result.stdout)
                            chrome_json_streams = []
                            for sink in sink_data:
                                props = sink.get('properties', {})
                                app_name = props.get('application.name', '')
                                if any(chrome_word in app_name.lower() for chrome_word in ['chrome', 'chromium']):
                                    chrome_json_streams.append({
                                        'index': sink.get('index'),
                                        'app_name': app_name,
                                        'sink': sink.get('sink'),
                                        'process_binary': props.get('application.process.binary', ''),
                                        'media_role': props.get('media.role', '')
                                    })
                            
                            if chrome_json_streams:
                                logger.info(f"✓ JSON format detected {len(chrome_json_streams)} Chrome streams:")
                                for stream in chrome_json_streams:
                                    logger.info(f"  Chrome #{stream['index']}: {stream['app_name']} -> {stream['sink']}")
                            else:
                                logger.info("✗ JSON format found no Chrome streams")
                        except json.JSONDecodeError:
                            logger.debug("JSON parsing failed for sink inputs")
                    else:
                        logger.debug("JSON format not supported by this pactl version")
                except:
                    logger.debug("pactl JSON format not available")
            
            # 5. Check PulseAudio clients
            clients = subprocess.run(["pactl", "list", "clients"], capture_output=True, text=True, timeout=5)
            if clients.returncode == 0:
                chrome_clients = []
                for line in clients.stdout.split('\n'):
                    if any(chrome_word in line.lower() for chrome_word in ['chrome', 'chromium']):
                        chrome_clients.append(line.strip())
                
                logger.info(f"{'✓' if chrome_clients else '✗'} Chrome PulseAudio clients: {len(chrome_clients)}")
                for client in chrome_clients[:5]:  # Limit output
                    logger.info(f"  {client}")
            
            # 6. Check environment variables
            env_vars = ['PULSE_SERVER', 'PULSE_SINK', 'XDG_RUNTIME_DIR', 'PULSE_RUNTIME_PATH', 'PULSE_RUNTIME_DIR']
            logger.info("Environment variables:")
            for var in env_vars:
                value = os.environ.get(var, 'NOT SET')
                logger.info(f"  {var}={value}")
            
            # 7. Test ALSA→Pulse bridge configuration
            logger.info("Testing ALSA→Pulse bridge...")
            try:
                # Check if ALSA is configured to use Pulse
                alsa_config_check = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=5)
                if alsa_config_check.returncode == 0:
                    logger.info("✓ ALSA devices available")
                else:
                    logger.warning("✗ ALSA device listing failed")
                
                # Check ALSA configuration
                asound_paths = ['/etc/asound.conf', '/home/nonroot/.asoundrc']
                for path in asound_paths:
                    if os.path.exists(path):
                        with open(path, 'r') as f:
                            content = f.read()
                            if 'type pulse' in content:
                                logger.info(f"✓ ALSA→Pulse bridge configured in {path}")
                            else:
                                logger.warning(f"✗ No Pulse config found in {path}")
                    else:
                        logger.debug(f"ALSA config file not found: {path}")
                        
            except Exception as e:
                logger.warning(f"ALSA bridge test failed: {e}")
            
        except Exception as e:
            logger.error(f"Diagnostic failed: {e}")
        
        logger.info("=== End Chrome Audio Diagnostics ===")

    def detect_chrome_audio_format(self):
        """
        Detect the audio format Chrome is using to optimize capture settings.
        This helps fine-tune FFmpeg parameters for the actual Chrome stream.
        """
        try:
            logger.info("Detecting Chrome audio format...")
            
            # Get detailed info about ChromeSink.monitor
            result = subprocess.run([
                "pactl", "list", "sources"
            ], capture_output=True, text=True, timeout=5)
            
            if result.returncode != 0:
                logger.warning("Could not get source information")
                return None
                
            # Look for ChromeSink.monitor details
            chromesink_info = {}
            in_chromesink_block = False
            
            for line in result.stdout.split('\n'):
                if "ChromeSink.monitor" in line:
                    in_chromesink_block = True
                    continue
                elif line.startswith("Source #") and in_chromesink_block:
                    break
                elif in_chromesink_block:
                    if "Sample Specification:" in line:
                        chromesink_info['sample_spec'] = line.strip()
                    elif "Channel Map:" in line:
                        chromesink_info['channel_map'] = line.strip()
                    elif "Volume:" in line:
                        chromesink_info['volume'] = line.strip()
            
            if chromesink_info:
                logger.info("ChromeSink.monitor audio format:")
                for key, value in chromesink_info.items():
                    logger.info(f"  {key}: {value}")
                return chromesink_info
            else:
                logger.warning("Could not detect ChromeSink.monitor format")
                return None
                
        except Exception as e:
            logger.warning(f"Audio format detection failed: {e}")
            return None

    def monitor_recording_quality(self):
        """
        Monitor the recording quality in real-time by checking audio levels
        and file growth. Useful for detecting issues during long recordings.
        """
        try:
            if not self.ffmpeg_proc or self.ffmpeg_proc.poll() is not None:
                return {"status": "not_recording", "error": "FFmpeg not running"}
            
            # Check file growth
            file_stats = {}
            if self.file_location and os.path.exists(self.file_location):
                file_size = os.path.getsize(self.file_location)
                duration = time.time() - self.recording_started_time if self.recording_started_time else 0
                file_stats = {
                    "file_size_mb": round(file_size / (1024 * 1024), 2),
                    "duration_sec": round(duration, 1),
                    "growth_rate_mb_per_min": round((file_size / (1024 * 1024)) / (duration / 60), 2) if duration > 0 else 0
                }
            
            # Quick audio level check
            audio_stats = {}
            if self.monitor_has_signal(timeout_sec=1):
                audio_stats["signal_present"] = True
                
                # Get current volume level
                try:
                    vol_result = subprocess.run([
                        "pactl", "list", "sources"
                    ], capture_output=True, text=True, timeout=2)
                    
                    if "ChromeSink.monitor" in vol_result.stdout:
                        # Extract volume info if available
                        lines = vol_result.stdout.split('\n')
                        for i, line in enumerate(lines):
                            if "ChromeSink.monitor" in line:
                                # Look for volume info in next few lines
                                for j in range(i+1, min(i+10, len(lines))):
                                    if "Volume:" in lines[j]:
                                        audio_stats["volume_info"] = lines[j].strip()
                                        break
                                break
                except:
                    pass
            else:
                audio_stats["signal_present"] = False
            
            return {
                "status": "recording",
                "file_stats": file_stats,
                "audio_stats": audio_stats,
                "timestamp": time.time()
            }
            
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def optimize_audio_processing_pipeline(self):
        """
        Optimize the audio processing pipeline based on detected system capabilities.
        This can improve performance on different hardware configurations.
        """
        try:
            logger.info("Optimizing audio processing pipeline...")
            
            # Detect CPU capabilities
            cpu_info = {}
            try:
                with open('/proc/cpuinfo', 'r') as f:
                    cpu_data = f.read()
                    cpu_info['cores'] = cpu_data.count('processor\t:')
                    cpu_info['has_sse2'] = 'sse2' in cpu_data
                    cpu_info['has_avx'] = 'avx' in cpu_data
            except:
                cpu_info = {'cores': 2, 'has_sse2': True, 'has_avx': False}  # Conservative defaults
            
            # Optimize based on capabilities
            optimizations = []
            
            # Thread count optimization
            if cpu_info['cores'] >= 4:
                os.environ['PULSE_LATENCY_MSEC'] = '30'  # Lower latency for powerful systems
                optimizations.append("Low latency mode (30ms)")
            else:
                os.environ['PULSE_LATENCY_MSEC'] = '60'  # Higher latency for weaker systems
                optimizations.append("Standard latency mode (60ms)")
            
            # FFmpeg threading optimization
            if cpu_info['cores'] >= 8:
                self.ffmpeg_thread_count = min(4, cpu_info['cores'] // 2)
                optimizations.append(f"Multi-threading ({self.ffmpeg_thread_count} threads)")
            else:
                self.ffmpeg_thread_count = 2
                optimizations.append("Conservative threading (2 threads)")
            
            # Audio processing optimizations
            if cpu_info.get('has_avx'):
                self.audio_resampler = 'speex-float-5'  # High quality for AVX systems
                optimizations.append("High-quality resampling (AVX)")
            elif cpu_info.get('has_sse2'):
                self.audio_resampler = 'speex-float-3'  # Medium quality for SSE2
                optimizations.append("Medium-quality resampling (SSE2)")
            else:
                self.audio_resampler = 'speex-float-1'  # Low CPU usage
                optimizations.append("Fast resampling (fallback)")
            
            logger.info(f"Applied optimizations: {', '.join(optimizations)}")
            return True
            
        except Exception as e:
            logger.warning(f"Pipeline optimization failed: {e}")
            return False

    def auto_adjust_audio_levels(self):
        """
        Automatically adjust ChromeSink volume based on detected audio levels.
        This helps ensure consistent recording volume across different meetings.
        """
        try:
            logger.info("Auto-adjusting ChromeSink audio levels...")
            
            # Sample audio for a few seconds to get baseline levels
            sample_duration = 3
            logger.info(f"Sampling audio for {sample_duration} seconds...")
            
            # Use FFmpeg to analyze volume levels
            result = subprocess.run([
                "ffmpeg", "-hide_banner", "-nostats", "-y",
                "-f", "pulse", "-i", "ChromeSink.monitor",
                "-t", str(sample_duration), "-vn", 
                "-af", "volumedetect,astats=metadata=1",
                "-f", "null", "-"
            ], capture_output=True, text=True, timeout=sample_duration + 5)
            
            if result.returncode == 0:
                # Parse volume information
                volume_info = {}
                for line in result.stderr.split('\n'):
                    if 'max_volume:' in line:
                        try:
                            vol_db = float(line.split('max_volume:')[1].split('dB')[0].strip())
                            volume_info['max_volume_db'] = vol_db
                        except:
                            pass
                    elif 'mean_volume:' in line:
                        try:
                            vol_db = float(line.split('mean_volume:')[1].split('dB')[0].strip())
                            volume_info['mean_volume_db'] = vol_db
                        except:
                            pass
                
                if volume_info:
                    # Adjust ChromeSink volume based on levels
                    max_vol = volume_info.get('max_volume_db', -20)
                    mean_vol = volume_info.get('mean_volume_db', -30)
                    
                    # Target: max around -6dB, mean around -20dB
                    if max_vol < -20:  # Too quiet
                        new_volume = "150%"
                        logger.info(f"Audio too quiet (max: {max_vol}dB), increasing to 150%")
                    elif max_vol > -3:  # Too loud
                        new_volume = "75%"
                        logger.info(f"Audio too loud (max: {max_vol}dB), decreasing to 75%")
                    else:  # Good level
                        new_volume = "100%"
                        logger.info(f"Audio levels good (max: {max_vol}dB), keeping at 100%")
                    
                    # Apply the adjustment
                    subprocess.run([
                        "pactl", "set-sink-volume", "ChromeSink", new_volume
                    ], check=True, timeout=5)
                    
                    logger.info(f"✓ Set ChromeSink volume to {new_volume}")
                    return True
                else:
                    logger.warning("Could not parse volume information")
                    return False
            else:
                logger.warning(f"Volume analysis failed: {result.stderr}")
                return False
                
        except Exception as e:
            logger.warning(f"Auto audio level adjustment failed: {e}")
            return False

    def bulletproof_audio_setup_sequence(self):
        """
        Execute the bulletproof audio setup sequence per your guidance:
        1. Set Pulse env
        2. Start Pulse
        3. Create null sink (48k stereo)
        4. Launch Chrome (caller responsibility)
        5. Poll & move inputs
        
        Call this method, then start Chrome, then call ensure_chrome_audio_capture().
        """
        logger.info("=== BULLETPROOF AUDIO SETUP SEQUENCE ===")
        
        # 0) Set comprehensive Pulse environment
        os.environ.update({
            'XDG_RUNTIME_DIR': '/run/user/1000',
            'PULSE_RUNTIME_DIR': '/run/user/1000/pulse', 
            'PULSE_SERVER': 'unix:/run/user/1000/pulse/native',
            'PULSE_SINK': 'ChromeSink',
            'PULSE_LATENCY_MSEC': '60'
        })
        logger.info("✓ Set comprehensive PulseAudio environment")
        
        # 1) Start Pulse (if not already)
        subprocess.run(["pulseaudio", "-D", "--exit-idle-time=-1"], capture_output=True)
        time.sleep(1)
        logger.info("✓ Started PulseAudio daemon")
        
        # 2) Create bulletproof null sink
        subprocess.run(["pactl", "unload-module", "module-null-sink"], capture_output=True)
        subprocess.run(["pactl", "unload-module", "module-loopback"], capture_output=True)
        
        result = subprocess.run([
            "pactl", "load-module", "module-null-sink",
            "sink_name=ChromeSink", 
            "sink_properties=device.description=ChromeSink",
            "rate=48000", "channels=2"
        ], capture_output=True)
        
        if result.returncode == 0:
            logger.info("✓ Created bulletproof ChromeSink (48kHz stereo)")
        else:
            logger.error(f"✗ Failed to create ChromeSink: {result.stderr.decode()}")
            return False
        
        # Set as default
        subprocess.run(["pactl", "set-default-sink", "ChromeSink"], capture_output=True)
        
        # 3) Verify setup including ALSA bridge
        time.sleep(1)
        
        # First verify ALSA→Pulse bridge is working
        alsa_bridge_ok = self.verify_alsa_bridge_configuration()
        if not alsa_bridge_ok:
            logger.warning("✗ ALSA→Pulse bridge verification failed, Chrome may not connect")
        
        if self.ensure_sink_and_monitor_exist():
            logger.info("✓ ChromeSink setup verified")
            if alsa_bridge_ok:
                logger.info("✓ ALSA→Pulse bridge configured - Chrome should connect to PulseAudio")
            logger.info("→ Now start Chrome, then call ensure_chrome_audio_capture()")
            return True
        else:
            logger.error("✗ ChromeSink verification failed")
            return False

    def test_chrome_audio_setup(self):
        """
        Test if Chrome audio setup is working with comprehensive diagnostics.
        Returns True if test sound can be recorded from ChromeSink.monitor.
        """
        try:
            logger.info("Testing Chrome audio setup with comprehensive diagnostics")
            
            # Quick 3-second parecord test (per your guidance)
            logger.info("Running 3-second parecord test...")
            test_file = "/tmp/chromesink_test.wav"
            
            parecord_result = subprocess.run([
                "parecord", 
                "--device=ChromeSink.monitor", 
                "--channels=2", 
                "--rate=48000", 
                "--duration=3",
                test_file
            ], capture_output=True, text=True, timeout=10)
            
            if parecord_result.returncode == 0 and os.path.exists(test_file):
                # Check file properties
                file_info = subprocess.run(["file", test_file], capture_output=True, text=True)
                ffprobe_info = subprocess.run([
                    "ffprobe", "-hide_banner", "-v", "quiet", "-show_format", "-show_streams", test_file
                ], capture_output=True, text=True)
                
                logger.info(f"✓ parecord test successful: {file_info.stdout.strip()}")
                logger.info(f"Audio properties: {ffprobe_info.stdout[:200]}")
                
                # Clean up test file
                os.remove(test_file)
                return True
            else:
                logger.warning(f"✗ parecord test failed: {parecord_result.stderr}")
                return False
                
        except Exception as e:
            logger.warning(f"parecord test failed: {e}")
            return False

    def live_vu_meter_test(self, duration_sec=5):
        """
        Run a live VU meter test to prove ChromeSink is receiving audio.
        This helps debug if Meet audio is actually flowing.
        """
        try:
            logger.info(f"Running live VU meter test for {duration_sec} seconds...")
            logger.info("(You should see steady byte flow when someone speaks in Meet)")
            
            # Use pacat to record from ChromeSink.monitor and pv to show throughput
            vu_cmd = f"timeout {duration_sec} pacat --record --device=ChromeSink.monitor | pv > /dev/null"
            
            result = subprocess.run(vu_cmd, shell=True, capture_output=True, text=True)
            
            # pv output goes to stderr, check for data flow
            if result.stderr.strip():
                logger.info(f"✓ VU meter detected data flow: {result.stderr.strip()}")
                return True
            else:
                logger.warning("✗ VU meter detected no data flow - ChromeSink appears silent")
                return False
                
        except Exception as e:
            logger.warning(f"VU meter test failed: {e}")
            return False

    def quick_audio_diagnostics(self):
        """
        Run quick audio diagnostics to check ChromeSink health.
        This is faster than the full diagnostic and good for health checks.
        """
        logger.info("=== Quick Audio Diagnostics ===")
        
        try:
            # 1. Check PulseAudio status
            pulse_info = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=3)
            if pulse_info.returncode == 0:
                logger.info("✓ PulseAudio running")
            else:
                logger.error("✗ PulseAudio not running")
                return False
            
            # 2. Check ChromeSink exists
            sinks = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True, timeout=3)
            chromesink_exists = "ChromeSink" in sinks.stdout if sinks.returncode == 0 else False
            logger.info(f"{'✓' if chromesink_exists else '✗'} ChromeSink exists")
            
            # 3. Check ChromeSink.monitor exists
            sources = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=3)
            monitor_exists = "ChromeSink.monitor" in sources.stdout if sources.returncode == 0 else False
            logger.info(f"{'✓' if monitor_exists else '✗'} ChromeSink.monitor exists")
            
            # 4. Quick signal check
            if monitor_exists:
                signal_present = self.monitor_has_signal(timeout_sec=2)
                logger.info(f"{'✓' if signal_present else '✗'} Audio signal detected")
                return signal_present
            
            return chromesink_exists and monitor_exists
            
        except Exception as e:
            logger.error(f"Quick diagnostics failed: {e}")
            return False
        finally:
            logger.info("=== End Quick Diagnostics ===")

    def verify_alsa_bridge_configuration(self):
        """Verify ALSA→Pulse bridge is properly configured and working"""
        try:
            logger.info("=== Verifying ALSA→Pulse Bridge Configuration ===")
            
            # 1. Check if /etc/asound.conf exists and has pulse config
            try:
                with open('/etc/asound.conf', 'r') as f:
                    asound_content = f.read()
                    has_pulse_config = 'type pulse' in asound_content
                    logger.info(f"{'✓' if has_pulse_config else '✗'} /etc/asound.conf has pulse configuration")
            except FileNotFoundError:
                logger.warning("✗ /etc/asound.conf not found")
                has_pulse_config = False
            
            # 2. Check if user-specific .asoundrc exists
            asoundrc_path = os.path.expanduser('~/.asoundrc')
            try:
                with open(asoundrc_path, 'r') as f:
                    asoundrc_content = f.read()
                    has_user_config = 'type pulse' in asoundrc_content
                    logger.info(f"{'✓' if has_user_config else '✗'} ~/.asoundrc has pulse configuration")
            except FileNotFoundError:
                logger.warning("✗ ~/.asoundrc not found")
                has_user_config = False
            
            # 3. Test ALSA→Pulse routing by checking default ALSA device
            try:
                result = subprocess.run(['aplay', '-l'], capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    logger.info("✓ ALSA subsystem responding")
                else:
                    logger.warning("✗ ALSA subsystem not responding")
            except Exception as e:
                logger.warning(f"✗ ALSA test failed: {e}")
            
            # 4. Check if ALSA can see PulseAudio as default
            try:
                result = subprocess.run(['aplay', '--list-pcms'], capture_output=True, text=True, timeout=5)
                if result.returncode == 0 and 'pulse' in result.stdout.lower():
                    logger.info("✓ ALSA can see PulseAudio PCM devices")
                else:
                    logger.warning("✗ ALSA cannot see PulseAudio PCM devices")
            except Exception as e:
                logger.warning(f"✗ ALSA PCM test failed: {e}")
            
            # 5. Test if applications can route through ALSA→Pulse bridge
            # This simulates what Chrome would do
            try:
                # Try to query the default device which should route to Pulse
                result = subprocess.run(['aplay', '--dump-hw-params', '--device=default', '/dev/null'], 
                                     capture_output=True, text=True, timeout=3)
                if result.returncode == 0:
                    logger.info("✓ ALSA→Pulse bridge is functional")
                    return True
                else:
                    logger.warning(f"✗ ALSA→Pulse bridge test failed: {result.stderr}")
                    return False
            except Exception as e:
                logger.warning(f"✗ ALSA→Pulse bridge test error: {e}")
                return False
                
        except Exception as e:
            logger.error(f"ALSA bridge verification failed: {e}")
            return False
        finally:
            logger.info("=== End ALSA Bridge Verification ===")

    def test_chrome_audio_setup(self):
        """
        Test if Chrome audio setup is working by playing a test sound.
        Returns True if test sound can be recorded from ChromeSink.monitor.
        """
        try:
            logger.info("Testing Chrome audio setup with test sound")
            
            # Play a test sound to ChromeSink
            test_cmd = [
                "paplay", "--device=ChromeSink", "/usr/share/sounds/alsa/Front_Center.wav"
            ]
            
            # Try to find a test sound file
            test_files = [
                "/usr/share/sounds/alsa/Front_Center.wav",
                "/usr/share/sounds/sound-icons/bell.wav", 
                "/usr/share/sounds/generic.wav"
            ]
            
            test_file = None
            for f in test_files:
                if os.path.exists(f):
                    test_file = f
                    break
            
            if not test_file:
                logger.warning("No test sound file found, generating tone")
                # Generate a test tone using speaker-test
                result = subprocess.run([
                    "timeout", "2", "speaker-test", "-t", "sine", "-f", "1000", "-l", "1", "-D", "ChromeSink"
                ], capture_output=True, timeout=5)
                if result.returncode == 0:
                    logger.info("✓ Generated test tone to ChromeSink")
                else:
                    logger.warning("Could not generate test tone")
                    return False
            else:
                result = subprocess.run([
                    "paplay", "--device=ChromeSink", test_file
                ], timeout=5)
                if result.returncode == 0:
                    logger.info(f"✓ Played test sound to ChromeSink: {test_file}")
                else:
                    logger.warning(f"Could not play test sound: {test_file}")
                    return False
            
            # Quick check if we can record from ChromeSink.monitor
            time.sleep(0.5)  # Let the sound settle
            if self.monitor_has_signal(timeout_sec=2):
                logger.info("✓ ChromeSink.monitor has audio signal - setup working!")
                return True
            else:
                logger.warning("✗ ChromeSink.monitor has no signal after test")
                return False
                
        except Exception as e:
            logger.warning(f"Audio setup test failed: {e}")
            return False
    
    def _test_audio_input(self, audio_cmd, description):
        """Test if a specific audio input configuration works"""
        try:
            # Create a quick test command to check if audio input works
            test_cmd = ["ffmpeg", "-y"] + audio_cmd + ["-t", "1.0", "-f", "null", "-"]
            
            # Run the test command with a short timeout
            result = subprocess.run(
                test_cmd, 
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.PIPE, 
                timeout=5,  # Increased timeout for more thorough testing
                text=True
            )
            
            # Check if the command succeeded (exit code 0) and didn't have critical errors
            if result.returncode == 0:
                return True
            
            # Check stderr for specific error patterns that indicate failures
            stderr_output = result.stderr.lower()
            
            # These are definitive failure patterns
            failure_patterns = [
                "input/output error",
                "no such file or directory", 
                "no such process",
                "cannot open audio device",
                "connection refused",
                "permission denied"
            ]
            
            if any(pattern in stderr_output for pattern in failure_patterns):
                logger.debug(f"Audio test failed for {description}: {stderr_output[:200]}")
                return False
                
            # For non-zero exit codes, be more conservative
            if result.returncode != 0:
                logger.debug(f"Audio test returned non-zero exit code {result.returncode} for {description}")
                return False
                
            # If we get here, it might be recoverable
            return True
            
        except subprocess.TimeoutExpired:
            logger.debug(f"Audio test timed out for {description}")
            return False
        except Exception as e:
            logger.debug(f"Audio test failed for {description}: {e}")
            return False

    def setup_pulseaudio_for_meeting_capture(self):
        """
        Set up PulseAudio for capturing meeting audio in headless environments.
        Creates a bulletproof null sink that Chrome can output to with zero jitter.
        """
        try:
            # Check if PulseAudio is available
            result = subprocess.run(["pactl", "info"], capture_output=True, timeout=5)
            if result.returncode != 0:
                logger.info("PulseAudio not available, skipping meeting audio setup")
                return False
                
            logger.info("Setting up bulletproof PulseAudio null sink for meeting audio capture")
            
            # Clean up any existing modules first to avoid conflicts
            logger.info("Cleaning up existing audio modules")
            subprocess.run(["pactl", "unload-module", "module-null-sink"], capture_output=True, timeout=5)
            subprocess.run(["pactl", "unload-module", "module-loopback"], capture_output=True, timeout=5)
            
            # Check if ChromeSink already exists after cleanup
            if self._check_pulseaudio_chromesink_exists():
                logger.info("ChromeSink still exists after cleanup, using existing")
                # Still set as default to ensure Chrome uses it
                try:
                    subprocess.run(["pactl", "set-default-sink", "ChromeSink"], check=True, timeout=10)
                    logger.info("Set existing ChromeSink as default audio output")
                except subprocess.CalledProcessError:
                    logger.warning("Could not set existing ChromeSink as default")
                return self.ensure_sink_and_monitor_exist()
            
            # Create null sink with explicit 48k stereo format (matches Meet)
            # This prevents format/clock drift that causes white noise
            logger.info("Creating null sink with 48kHz stereo format")
            result = subprocess.run([
                "pactl", "load-module", "module-null-sink",
                "sink_name=ChromeSink",
                "sink_properties=device.description=ChromeSink",
                "rate=48000",
                "channels=2"
            ], capture_output=True, timeout=10)
            
            if result.returncode != 0:
                logger.error(f"Failed to create ChromeSink: {result.stderr.decode()}")
                return False
            
            logger.info("✓ Created ChromeSink null sink (48kHz stereo, zero jitter)")
            
            # Set ChromeSink as default output (so Chrome uses it)
            subprocess.run([
                "pactl", "set-default-sink", "ChromeSink"
            ], check=True, timeout=10)
            logger.info("✓ Set ChromeSink as default audio output")
            
            # Set reasonable volume level
            try:
                subprocess.run([
                    "pactl", "set-sink-volume", "ChromeSink", "100%"
                ], check=True, timeout=10)
                logger.info("✓ Set ChromeSink volume to 100%")
            except subprocess.CalledProcessError:
                logger.warning("Could not set sink volume")
            
            # Wait for sink and monitor to be properly registered
            if self.ensure_sink_and_monitor_exist():
                logger.info("✓ ChromeSink setup completed successfully")
                return True
            else:
                logger.error("✗ ChromeSink setup failed - verification failed")
                return False
            
        except subprocess.CalledProcessError as e:
            logger.warning(f"Failed to set up PulseAudio meeting capture: {e}")
            return False
        except subprocess.TimeoutExpired:
            logger.warning("PulseAudio setup timed out")
            return False
        except Exception as e:
            logger.warning(f"Error setting up PulseAudio: {e}")
            return False

    def start_pulseaudio_if_needed(self):
        """Start PulseAudio daemon if not already running with optimized settings"""
        try:
            # Check if PulseAudio is already running
            result = subprocess.run(["pactl", "info"], capture_output=True, timeout=5)
            if result.returncode == 0:
                logger.info("PulseAudio is already running")
                return True
                
            logger.info("Starting PulseAudio daemon with basic settings")
            # Start PulseAudio in daemon mode with basic settings for maximum compatibility
            subprocess.run([
                "pulseaudio", "-D", 
                "--exit-idle-time=-1"   # Never exit - this is the only widely supported option
            ], check=True, timeout=10)
            
            # Wait a moment for it to start
            time.sleep(2)
            
            # Verify it's running
            result = subprocess.run(["pactl", "info"], capture_output=True, timeout=5)
            if result.returncode == 0:
                logger.info("PulseAudio started successfully with basic settings")
                return True
            else:
                logger.warning("PulseAudio failed to start properly")
                return False
                
        except subprocess.CalledProcessError as e:
            logger.warning(f"Failed to start PulseAudio: {e}")
            return False
        except Exception as e:
            logger.warning(f"Error starting PulseAudio: {e}")
            return False

    def start_recording(self, display_var):
        logger.info(f"Starting screen recorder for display {display_var} with dimensions {self.screen_dimensions} and file location {self.file_location}")
        
        # Create a log file for ffmpeg output to help with debugging
        if self.file_location:
            log_dir = os.path.dirname(self.file_location)
            log_filename = f"ffmpeg_{os.path.basename(self.file_location)}.log"
            self.ffmpeg_log_file = os.path.join(log_dir, log_filename)

        if self.audio_only:
            # For audio-only recording, we must have audio input
            audio_options = self._get_audio_input_options()
            if not audio_options:
                raise RuntimeError("No audio input available for audio-only recording")
                
            # FFmpeg command for audio-only recording to MP3
            ffmpeg_cmd = [
                "ffmpeg",
                "-y",  # Overwrite output file without asking
            ] + audio_options + [
                "-c:a",
                "libmp3lame",  # MP3 codec
                "-b:a",
                "192k",  # Audio bitrate (192 kbps for good quality)
                "-ar",
                "44100",  # Sample rate
                "-ac",
                "1",  # Mono
                self.file_location,
            ]
            
            # Try to start recording
            self._attempt_recording(ffmpeg_cmd, display_var)
        else:
            # For video recording, try with audio first, then fallback to video-only
            audio_options = self._get_audio_input_options()
            
            if audio_options:
                # Try with audio first - AUDIO INPUT FIRST for better A/V sync
                ffmpeg_cmd = [
                    "ffmpeg", "-y",
                ] + audio_options + [
                    "-thread_queue_size", "4096", 
                    "-framerate", "30", 
                    "-video_size", f"{self.screen_dimensions[0]}x{self.screen_dimensions[1]}", 
                    "-f", "x11grab", "-draw_mouse", "0", "-probesize", "500k", "-i", display_var,
                    "-filter:a", "aresample=async=1:min_hard_comp=0.100:first_pts=0,aformat=sample_fmts=s16:sample_rates=48000:channel_layouts=stereo",
                    "-vf", f"crop={self.recording_dimensions[0]}:{self.recording_dimensions[1]}:10:10", 
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "30", 
                    "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                    self.file_location
                ]
                
                # Try recording with audio
                if self._attempt_recording(ffmpeg_cmd, display_var, allow_fallback=True):
                    return  # Success!
                
                # If failed and fallback allowed, try video-only
                logger.warning("Audio recording failed, falling back to video-only recording")
            
            # Video-only recording (either by choice or fallback)
            logger.info("Recording video only (no audio)")
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-thread_queue_size", "4096", 
                "-framerate", "30", 
                "-video_size", f"{self.screen_dimensions[0]}x{self.screen_dimensions[1]}", 
                "-f", "x11grab", "-draw_mouse", "0", "-probesize", "500k", "-i", display_var,
                "-vf", f"crop={self.recording_dimensions[0]}:{self.recording_dimensions[1]}:10:10", 
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "30", 
                self.file_location
            ]
            
            # Final attempt - this should not fail
            self._attempt_recording(ffmpeg_cmd, display_var, allow_fallback=False)

    def _attempt_recording(self, ffmpeg_cmd, display_var, allow_fallback=False):
        """
        Attempt to start recording with the given FFmpeg command.
        Returns True if successful, False if failed and fallback is allowed.
        """
        logger.info(f"Starting FFmpeg command: {' '.join(ffmpeg_cmd)}")
        
        # Open log file for ffmpeg output
        log_file_handle = None
        if self.ffmpeg_log_file:
            try:
                log_file_handle = open(self.ffmpeg_log_file, 'w')
                logger.info(f"FFmpeg output will be logged to: {self.ffmpeg_log_file}")
            except Exception as e:
                logger.warning(f"Could not create ffmpeg log file {self.ffmpeg_log_file}: {e}")
        
        # Start ffmpeg with proper error handling
        try:
            self.ffmpeg_proc = subprocess.Popen(
                ffmpeg_cmd, 
                stdout=log_file_handle if log_file_handle else subprocess.DEVNULL, 
                stderr=subprocess.STDOUT if log_file_handle else subprocess.DEVNULL
            )
            self.recording_started_time = time.time()
            logger.info(f"FFmpeg process started with PID: {self.ffmpeg_proc.pid}")
            
            # Give ffmpeg a moment to initialize
            time.sleep(2)  # Increased from 1 to 2 seconds for better detection
            
            # Check if process is still running after initialization
            if self.ffmpeg_proc.poll() is not None:
                return_code = self.ffmpeg_proc.returncode
                logger.error(f"FFmpeg process exited immediately with return code: {return_code}")
                
                # Read the error output
                error_output = ""
                if self.ffmpeg_log_file and os.path.exists(self.ffmpeg_log_file):
                    try:
                        with open(self.ffmpeg_log_file, 'r') as f:
                            error_output = f.read()
                            logger.error(f"FFmpeg error output: {error_output}")
                    except Exception as e:
                        logger.error(f"Could not read ffmpeg log file: {e}")
                
                # Check if this is an audio-related error that we can recover from
                if allow_fallback and self._is_audio_related_error(error_output):
                    logger.warning("Detected audio-related error, will attempt video-only fallback")
                    self.ffmpeg_proc = None
                    return False  # Indicate fallback should be attempted
                
                # For non-audio errors or when fallback not allowed, raise exception
                self.ffmpeg_proc = None
                raise RuntimeError(f"FFmpeg process failed to start (exit code: {return_code})")
            else:
                logger.info("FFmpeg process started successfully")
                return True  # Success
                
        except Exception as e:
            logger.error(f"Failed to start FFmpeg process: {e}")
            if allow_fallback and "audio" in str(e).lower():
                logger.warning("Audio-related error detected, will attempt fallback")
                return False
            raise
        finally:
            if log_file_handle:
                log_file_handle.close()

    def _is_audio_related_error(self, error_output):
        """Check if FFmpeg error output indicates an audio-related problem"""
        if not error_output:
            return False
            
        error_output_lower = error_output.lower()
        audio_error_patterns = [
            "no such process",
            "input/output error", 
            "alsa",
            "pulse",
            "audio device",
            "default:",
            "cannot open audio device",
            "no such file or directory",
            "chromesink.monitor",              # <— add this
            "device or resource busy",         # Common PulseAudio error
            "connection timed out",            # Network/socket issues
            "invalid argument",                # Format mismatch errors
        ]
        
        return any(pattern in error_output_lower for pattern in audio_error_patterns)

    # Pauses by muting the audio and showing a black xterm covering the entire screen
    def pause_recording(self):
        if self.paused:
            return True  # Already paused, consider this success

        try:
            sw, sh = self.screen_dimensions

            x, y = 0, 0

            self.xterm_proc = subprocess.Popen(["xterm", "-bg", "black", "-fg", "black", "-geometry", f"{sw}x{sh}+{x}+{y}", "-xrm", "*borderWidth:0", "-xrm", "*scrollBar:false"])

            subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1"], check=True)
            self.paused = True
            return True
        except Exception as e:
            logger.error(f"Failed to pause recording: {e}")
            return False

    # Resumes by unmuting the audio and killing the xterm proc
    def resume_recording(self):
        if not self.paused:
            return True

        try:
            self.xterm_proc.terminate()
            self.xterm_proc.wait()
            self.xterm_proc = None
            subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"], check=True)
            self.paused = False
            return True
        except Exception as e:
            logger.error(f"Failed to resume recording: {e}")
            return False

    def stop_recording(self):
        if not self.ffmpeg_proc:
            return
            
        logger.info(f"Stopping FFmpeg process (PID: {self.ffmpeg_proc.pid})")
        
        try:
            # First try graceful termination
            self.ffmpeg_proc.terminate()
            
            # Wait up to 5 seconds for graceful shutdown
            try:
                self.ffmpeg_proc.wait(timeout=5)
                logger.info("FFmpeg process terminated gracefully")
            except subprocess.TimeoutExpired:
                logger.warning("FFmpeg process did not terminate gracefully, forcing kill")
                self.ffmpeg_proc.kill()
                self.ffmpeg_proc.wait()
                
        except Exception as e:
            logger.error(f"Error stopping FFmpeg process: {e}")
            try:
                self.ffmpeg_proc.kill()
                self.ffmpeg_proc.wait()
            except:
                pass
        
        finally:
            self.ffmpeg_proc = None
            
        # Log recording duration and check file creation
        if self.recording_started_time:
            duration = time.time() - self.recording_started_time
            logger.info(f"Recording duration: {duration:.2f} seconds")
            
        if self.file_location and os.path.exists(self.file_location):
            file_size = os.path.getsize(self.file_location)
            logger.info(f"Recording file created: {self.file_location} ({file_size} bytes)")
        else:
            logger.warning(f"Recording file not found or empty: {self.file_location}")
            
        logger.info(f"Stopped screen and audio recorder for display with dimensions {self.screen_dimensions} and file location {self.file_location}")

    def check_recording_health(self):
        """Check if recording is healthy by monitoring file size and process status"""
        if not self.ffmpeg_proc:
            return False, "FFmpeg process not running"
            
        # Check if process is still alive
        if self.ffmpeg_proc.poll() is not None:
            return False, f"FFmpeg process exited with code {self.ffmpeg_proc.returncode}"
            
        # Check if file exists and is growing
        if self.file_location and os.path.exists(self.file_location):
            file_size = os.path.getsize(self.file_location)
            duration = time.time() - self.recording_started_time if self.recording_started_time else 0
            
            # For recordings longer than 10 seconds, expect at least some data
            if duration > 10 and file_size == 0:
                return False, f"No data written after {duration:.1f} seconds"
                
            return True, f"Recording healthy: {file_size} bytes after {duration:.1f}s"
        else:
            duration = time.time() - self.recording_started_time if self.recording_started_time else 0
            # Allow some time for file creation
            if duration > 5:
                return False, f"Recording file not created after {duration:.1f} seconds"
            return True, "Recording starting up"

    def get_seekable_path(self, path):
        """
        Transform a file path to include '.seekable' before the extension.
        Example: /tmp/file.webm -> /tmp/file.seekable.webm
        """
        base, ext = os.path.splitext(path)
        return f"{base}.seekable{ext}"

    def cleanup(self):
        input_path = self.file_location

        # If no input path at all, then we aren't trying to generate a file at all
        if input_path is None:
            return

        # Clean up ffmpeg log file if it exists
        if self.ffmpeg_log_file and os.path.exists(self.ffmpeg_log_file):
            try:
                # Log any final ffmpeg output before cleanup
                with open(self.ffmpeg_log_file, 'r') as f:
                    log_content = f.read().strip()
                    if log_content:
                        logger.info(f"Final FFmpeg log output: {log_content}")
                os.remove(self.ffmpeg_log_file)
                logger.info(f"Cleaned up FFmpeg log file: {self.ffmpeg_log_file}")
            except Exception as e:
                logger.warning(f"Could not clean up FFmpeg log file {self.ffmpeg_log_file}: {e}")

        # Check if input file exists
        if not os.path.exists(input_path):
            logger.warning(f"Input file does not exist at {input_path}")
            
            # Instead of creating an empty file, check if ffmpeg was killed unexpectedly
            if self.recording_started_time:
                duration = time.time() - self.recording_started_time
                logger.error(f"Recording was expected to run for {duration:.2f} seconds but no file was created")
                
                # Check for partial/temporary files that might exist
                temp_patterns = [
                    input_path + ".tmp",
                    input_path + ".part", 
                    input_path + "~"
                ]
                
                for temp_path in temp_patterns:
                    if os.path.exists(temp_path):
                        logger.info(f"Found partial recording file: {temp_path}")
                        try:
                            os.rename(temp_path, input_path)
                            logger.info(f"Recovered partial recording: {temp_path} -> {input_path}")
                            break
                        except Exception as e:
                            logger.error(f"Could not recover partial recording {temp_path}: {e}")
                
                # If still no file, create empty one as last resort
                if not os.path.exists(input_path):
                    logger.info(f"Creating empty file as fallback: {input_path}")
                    with open(input_path, "wb"):
                        pass  # Create empty file
            else:
                logger.info(f"Creating empty file: {input_path}")
                with open(input_path, "wb"):
                    pass  # Create empty file
            return

        # Log file size for debugging
        file_size = os.path.getsize(input_path)
        logger.info(f"Processing recording file: {input_path} ({file_size} bytes)")

        # if audio only, we don't need to make it seekable
        if self.audio_only:
            return

        # if input file is greater than 3 GB, we will skip seekability
        if file_size > 3 * 1024 * 1024 * 1024:
            logger.info("Input file is greater than 3 GB, skipping seekability")
            return
            
        # if file is too small (less than 1KB), skip seekability processing
        if file_size < 1024:
            logger.warning(f"Input file is very small ({file_size} bytes), skipping seekability processing")
            return

        output_path = self.get_seekable_path(self.file_location)
        # the file is seekable, so we don't need to make it seekable
        try:
            self.make_file_seekable(input_path, output_path)
        except Exception as e:
            logger.error(f"Failed to make file seekable: {e}")
            return

    def make_file_seekable(self, input_path, tempfile_path):
        """Use ffmpeg to move the moov atom to the beginning of the file."""
        logger.info(f"Making file seekable: {input_path} -> {tempfile_path}")
        # log how many bytes are in the file
        logger.info(f"File size: {os.path.getsize(input_path)} bytes")
        command = [
            "ffmpeg",
            "-i",
            str(input_path),  # Input file
            "-c",
            "copy",  # Copy streams without re-encoding
            "-avoid_negative_ts",
            "make_zero",  # Optional: Helps ensure timestamps start at or after 0
            "-movflags",
            "+faststart",  # Optimize for web playback
            "-y",  # Overwrite output file without asking
            str(tempfile_path),  # Output file
        ]

        result = subprocess.run(command, capture_output=True, text=True)

        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg failed to make file seekable: {result.stderr}")

        # Replace the original file with the seekable version
        try:
            os.replace(str(tempfile_path), str(input_path))
            logger.info(f"Replaced original file with seekable version: {input_path}")
        except Exception as e:
            logger.error(f"Failed to replace original file with seekable version: {e}")
            raise RuntimeError(f"Failed to replace original file: {e}")

import logging
import os
import subprocess
import time
import tempfile

logger = logging.getLogger(__name__)


class ScreenAndAudioRecorder:
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
        # Check if audio recording is explicitly disabled
        if os.environ.get('DISABLE_AUDIO_RECORDING', '').lower() in ('1', 'true', 'yes'):
            logger.info("Audio recording disabled by environment variable DISABLE_AUDIO_RECORDING")
            return None
            
        audio_options = []
        
        # Try different audio input methods in order of preference
        audio_methods = [
            # Method 1: Try default ALSA device
            (["-thread_queue_size", "4096", "-f", "alsa", "-i", "default"], "ALSA default"),
            
            # Method 2: Try PulseAudio if available
            (["-thread_queue_size", "4096", "-f", "pulse", "-i", "default"], "PulseAudio default"),
            
            # Method 3: Try specific ALSA device
            (["-thread_queue_size", "4096", "-f", "alsa", "-i", "hw:0"], "ALSA hw:0"),
            
            # Method 4: Generate silent audio (last resort)
            (["-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=44100"], "Silent audio generator"),
        ]
        
        for audio_cmd, description in audio_methods:
            if self._test_audio_input(audio_cmd, description):
                logger.info(f"Using audio input method: {description}")
                return audio_cmd
                
        logger.warning("No working audio input found, will record video only")
        return None
    
    def _test_audio_input(self, audio_cmd, description):
        """Test if a specific audio input configuration works"""
        try:
            # Create a quick test command to check if audio input works
            test_cmd = ["ffmpeg", "-y"] + audio_cmd + ["-t", "0.1", "-f", "null", "-"]
            
            # Run the test command with a short timeout
            result = subprocess.run(
                test_cmd, 
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.PIPE, 
                timeout=3,
                text=True
            )
            
            # Check if the command succeeded (exit code 0) and didn't have critical errors
            if result.returncode == 0:
                return True
            
            # Also check stderr for specific error patterns that might be recoverable
            stderr_output = result.stderr.lower()
            if "input/output error" in stderr_output or "no such file or directory" in stderr_output:
                return False
                
            # Some warnings are OK, as long as we didn't get a complete failure
            return "error" not in stderr_output
            
        except subprocess.TimeoutExpired:
            logger.debug(f"Audio test timed out for {description}")
            return False
        except Exception as e:
            logger.debug(f"Audio test failed for {description}: {e}")
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
        else:
            # Check if we should skip audio due to environment constraints
            audio_options = self._get_audio_input_options()
            
            if audio_options:
                # Include audio in recording
                ffmpeg_cmd = [
                    "ffmpeg", "-y", "-thread_queue_size", "4096", 
                    "-framerate", "30", 
                    "-video_size", f"{self.screen_dimensions[0]}x{self.screen_dimensions[1]}", 
                    "-f", "x11grab", "-draw_mouse", "0", "-probesize", "32", "-i", display_var,
                ] + audio_options + [
                    "-vf", f"crop={self.recording_dimensions[0]}:{self.recording_dimensions[1]}:10:10", 
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "30", 
                    "-c:a", "aac", "-strict", "experimental", "-b:a", "128k", 
                    self.file_location
                ]
            else:
                # Video-only recording (no audio)
                logger.warning("Audio device not available, recording video only")
                ffmpeg_cmd = [
                    "ffmpeg", "-y", "-thread_queue_size", "4096", 
                    "-framerate", "30", 
                    "-video_size", f"{self.screen_dimensions[0]}x{self.screen_dimensions[1]}", 
                    "-f", "x11grab", "-draw_mouse", "0", "-probesize", "32", "-i", display_var,
                    "-vf", f"crop={self.recording_dimensions[0]}:{self.recording_dimensions[1]}:10:10", 
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "30", 
                    self.file_location
                ]

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
            time.sleep(1)
            
            # Check if process is still running after initialization
            if self.ffmpeg_proc.poll() is not None:
                logger.error(f"FFmpeg process exited immediately with return code: {self.ffmpeg_proc.returncode}")
                if self.ffmpeg_log_file and os.path.exists(self.ffmpeg_log_file):
                    try:
                        with open(self.ffmpeg_log_file, 'r') as f:
                            error_output = f.read()
                            logger.error(f"FFmpeg error output: {error_output}")
                    except Exception as e:
                        logger.error(f"Could not read ffmpeg log file: {e}")
            else:
                logger.info("FFmpeg process started successfully")
                
        except Exception as e:
            logger.error(f"Failed to start FFmpeg process: {e}")
            if log_file_handle:
                log_file_handle.close()
            raise
        finally:
            if log_file_handle:
                log_file_handle.close()

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

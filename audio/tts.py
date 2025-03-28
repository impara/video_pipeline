"""
Text-to-Speech module supporting multiple providers:
- ElevenLabs API (default)
- UnrealSpeech API

Handles:
- Converting text scripts to natural-sounding voiceovers
- Managing different voices and speech parameters
- Saving generated audio files
- Providing word-level timing data for karaoke captions
"""

import os
import uuid
import json
import hashlib
import time
import re
import string
import logging
import tempfile
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any, Union
import hashlib
from pydub import AudioSegment
from pydub.generators import Sine

# Import the VoiceOptimizer
from core.error_handling import TTSError, retry_api_call, AudioGenerationError
from core.utils import ensure_directory_exists
from core.cache_handler import CacheHandler
from core.config import Config
from audio.voice_optimizer import VoiceOptimizer

# Import UnrealSpeech provider
from audio.unrealspeech_provider import UnrealSpeechProvider, UnrealSpeechError

# Check if UnrealSpeech is available
try:
    from audio.unrealspeech_provider import UnrealSpeechProvider
    HAS_UNREALSPEECH = True
except ImportError:
    HAS_UNREALSPEECH = False

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class TTSError(Exception):
    """Base exception class for Text-to-Speech errors."""
    pass

class TextToSpeech:
    def __init__(self, 
                api_key: Optional[str] = None, 
                use_smart_voice: bool = True,
                provider: str = "elevenlabs"):
        """Initialize the TTS client.
        
        Args:
            api_key: Optional API key. If not provided, will look for provider-specific env var.
            use_smart_voice: Whether to use smart voice optimization (default: True)
            provider: TTS provider to use ("elevenlabs" or "unrealspeech")
            
        Raises:
            ValueError: If API key is missing or invalid
        """
        self.provider_name = provider.lower()
        self.use_smart_voice = use_smart_voice
        
        # Initialize the appropriate provider
        if self.provider_name == "elevenlabs":
            self._init_elevenlabs(api_key)
        elif self.provider_name == "unrealspeech":
            self._init_unrealspeech(api_key)
        else:
            raise ValueError(f"Unsupported provider: {provider}. Supported providers: elevenlabs, unrealspeech")
        
        # Load config for output directories
        self.config = Config()
        self.output_dir = self.config.audio_output_dir
        ensure_directory_exists(self.output_dir)
        
        # Cache setup
        self.cache_file = self.output_dir / "tts_cache.json"
        self._load_cache()
        
        # Voice settings for different content types
        self.voice_settings = {
            "narrative": {
                "voice_settings": {
                    "stability": 0.7,
                    "similarity_boost": 0.8,
                    "style": 0.65,
                    "use_speaker_boost": True
                }
            },
            "descriptive": {
                "voice_settings": {
                    "stability": 0.85,
                    "similarity_boost": 0.7,
                    "style": 0.35,
                    "use_speaker_boost": True
                }
            },
            "dialogue": {
                "voice_settings": {
                    "stability": 0.6,
                    "similarity_boost": 0.9,
                    "style": 0.8,
                    "use_speaker_boost": True
                }
            }
        }
        
        # Initialize voice optimizer if smart voice is enabled
        if self.use_smart_voice:
            # Create a single instance of VoiceOptimizer
            self.voice_optimizer = VoiceOptimizer()
            # Adapt voice catalog to available voices
            self.voice_optimizer.adapt_to_available_voices(self.available_voice_names)
            logger.info("Smart voice optimization enabled")
            
        # Log available voices for reference
        logger.info(f"Available voices: {', '.join(self.available_voice_names)}")

    def _init_elevenlabs(self, api_key: Optional[str] = None):
        """Initialize ElevenLabs provider."""
        # Import elevenlabs dynamically to handle different package structures
        try:
            import elevenlabs
            from elevenlabs.client import ElevenLabs
            
            self.api_key = api_key or os.getenv("ELEVENLABS_API_KEY")
            if not self.api_key:
                raise ValueError("ELEVENLABS_API_KEY environment variable or api_key parameter is required")
            if not self.api_key.strip():
                raise ValueError("API key cannot be empty")
            
            # For testing purposes, accept keys with "test" in them
            is_test_key = "test" in self.api_key.lower()
            if not is_test_key and not (self.api_key.startswith("el_") or self.api_key.startswith("sk_")):
                raise ValueError("Invalid API key format. ElevenLabs API keys should start with 'el_' or 'sk_'")
            
            # Initialize test mode with sample voices if using test key
            if is_test_key:
                logger.warning("Using test API key - some features will be simulated")
                # Create a test voice collection
                # Handle different elevenlabs package versions
                try:
                    # Try newer package structure
                    from elevenlabs.api.models import Voice
                except ImportError:
                    try:
                        # Try older package structure
                        from elevenlabs.models import Voice
                    except ImportError:
                        # Create a simple Voice class for testing
                        class Voice:
                            def __init__(self, voice_id, name, category):
                                self.voice_id = voice_id
                                self.name = name
                                self.category = category
                
                self.available_voices = [
                    Voice(voice_id="test_daniel", name="Daniel", category="test"),
                    Voice(voice_id="test_rachel", name="Rachel", category="test")
                ]
            else:
                # Initialize ElevenLabs client using the newer approach
                self.client = ElevenLabs(api_key=self.api_key)
                # Try to list voices to verify API key works
                response = self.client.voices.get_all()
                self.available_voices = response.voices
                
                if not self.available_voices:
                    raise TTSError("No voices available. API key may be invalid or rate limited.")
                
                logger.info(f"Successfully connected to ElevenLabs API. Found {len(self.available_voices)} voices.")
            
            # Get available voice names
            self.available_voice_names = sorted([v.name for v in self.available_voices])
            
            logger.info(f"Available voices (sorted): {', '.join(self.available_voice_names)}")
            
            # Always use Daniel as the default voice
            self.default_voice = "Daniel"
            self.default_model = "eleven_turbo_v2"
            
            # Verify Daniel voice exists
            if self.default_voice not in self.available_voice_names:
                logger.warning(f"Default voice '{self.default_voice}' not found. Available voices: {', '.join(self.available_voice_names)}")
                # Fall back to first available voice
                self.default_voice = self.available_voice_names[0]
                logger.info(f"Using fallback voice: {self.default_voice}")
                
        except Exception as e:
            raise TTSError(f"Failed to initialize ElevenLabs client: {str(e)}")
    
    def _init_unrealspeech(self, api_key: Optional[str] = None):
        """Initialize UnrealSpeech provider."""
        try:
            # Initialize UnrealSpeech provider
            self.unrealspeech = UnrealSpeechProvider(api_key)
            
            # Get available voices
            self.available_voices = self.unrealspeech.get_available_voices()
            
            # Get available voice names
            self.available_voice_names = sorted([v["name"] for v in self.available_voices])
            logger.info(f"Available voices (sorted): {', '.join(self.available_voice_names)}")
            
            # Always use Daniel as the default voice
            self.default_voice = "Daniel"
            self.default_model = "default"  # UnrealSpeech doesn't have model selection
            
            # Verify Daniel voice exists
            if self.default_voice not in self.available_voice_names:
                logger.warning(f"Default voice '{self.default_voice}' not found. Available voices: {', '.join(self.available_voice_names)}")
                # Fall back to first available voice
                self.default_voice = self.available_voice_names[0]
                logger.info(f"Using fallback voice: {self.default_voice}")
                
        except Exception as e:
            raise TTSError(f"Failed to initialize UnrealSpeech client: {str(e)}")

    def _load_cache(self):
        """Load the TTS cache from disk."""
        try:
            if self.cache_file.exists():
                with open(self.cache_file, 'r') as f:
                    self.cache = json.load(f)
            else:
                self.cache = {}
        except Exception as e:
            logger.warning(f"Failed to load TTS cache: {e}")
            self.cache = {}
            
    def _save_cache(self):
        """Save the TTS cache to disk."""
        try:
            with open(self.cache_file, 'w') as f:
                json.dump(self.cache, f)
        except Exception as e:
            logger.warning(f"Failed to save TTS cache: {e}")
            
    def _get_cache_key(self, text: str, voice: str, settings: Dict = None) -> str:
        """Generate a cache key from text, voice, and settings."""
        # Normalize text (strip whitespace, lowercase)
        text = text.strip().lower()
        # Remove extra whitespace
        text = ' '.join(text.split())
        voice = voice.strip().lower()
        
        # Include provider in cache key to avoid conflicts
        provider_prefix = f"provider={self.provider_name}|"
        
        # Create key string with voice settings if provided
        if settings:
            # Sort settings to ensure consistent order
            settings_str = json.dumps(settings, sort_keys=True)
            key_str = f"{provider_prefix}{text}|{voice}|{settings_str}"
        else:
            key_str = f"{provider_prefix}{text}|{voice}"
            
        return hashlib.md5(key_str.encode()).hexdigest()
        
    def _get_cached_audio(self, cache_key: str) -> Optional[Dict]:
        """Check if audio exists in cache."""
        if cache_key in self.cache:
            cache_data = self.cache[cache_key]
            audio_path = Path(cache_data["path"])
            if audio_path.exists():
                logger.info(f"Found cached audio: {audio_path}")
                return {
                    "path": str(audio_path),
                    "word_timings": cache_data.get("word_timings", []),
                    "duration": cache_data.get("duration", 0.0)
                }
            else:
                # Clean up invalid cache entry
                del self.cache[cache_key]
                self._save_cache()
        return None

    def _detect_content_type(self, text: str) -> str:
        """
        Detect the type of content to optimize voice settings.
        
        Args:
            text: The text to analyze
            
        Returns:
            str: Content type ('narrative', 'descriptive', or 'dialogue')
        """
        # Check for scene description markers
        if any(marker in text.lower() for marker in ["scene", "visual", "we see", "camera shows"]):
            return "descriptive"
        
        # Check for dialogue markers
        if any(marker in text for marker in ['"', "'", ":", "says", "exclaims", "asks"]):
            return "dialogue"
            
        # Default to narrative for other content
        return "narrative"

    def generate_speech(self, text: str, voice_name: str = None, use_smart_voice: bool = None) -> Dict:
        """
        Converts text to speech using the selected provider with optimized settings.
        Always uses the Daniel voice with optimized parameters based on content.
        
        Args:
            text: The text to convert to speech
            voice_name: Parameter kept for backward compatibility (ignored, always uses Daniel)
            use_smart_voice: Override the default smart voice setting for parameter optimization
        Returns:
            Dict: Contains path to the generated audio file and word timing data
                {
                    "path": str,  # Path to audio file
                    "word_timings": List[Tuple[str, float, float]]  # (word, start_time, end_time)
                }
        Raises:
            TTSError: If speech generation or saving fails
        """
        if not text.strip():
            raise TTSError("Cannot generate speech from empty text")

        try:
            # Normalize text to ensure consistent analysis
            normalized_text = text.strip()
            
            # Determine whether to use smart voice optimization for parameters
            should_use_smart_voice = use_smart_voice if use_smart_voice is not None else self.use_smart_voice
            
            # Always use Daniel voice
            voice_name = "Daniel"
            logger.info(f"Using Daniel voice")
            
            # Get voice parameters
            if should_use_smart_voice:
                # Use smart voice optimization for parameters only
                logger.info(f"Using smart parameter optimization for text: '{normalized_text[:50]}...'")
                optimized_params = self.voice_optimizer.optimize_voice_parameters(normalized_text)
                
                if self.provider_name == "elevenlabs":
                    settings = self.voice_optimizer.get_voice_settings_dict(optimized_params)
                    model = optimized_params["model"]
                elif self.provider_name == "unrealspeech":
                    # Map ElevenLabs settings to UnrealSpeech settings
                    elevenlabs_settings = self.voice_optimizer.get_voice_settings_dict(optimized_params)
                    settings = self.unrealspeech.map_voice_settings(elevenlabs_settings)
                    model = "default"  # UnrealSpeech doesn't have model selection
            else:
                # Get content-specific voice settings
                content_type = self._detect_content_type(normalized_text)
                
                if self.provider_name == "elevenlabs":
                    settings = self.voice_settings[content_type]["voice_settings"]
                    model = self.default_model
                elif self.provider_name == "unrealspeech":
                    # Map ElevenLabs settings to UnrealSpeech settings
                    elevenlabs_settings = self.voice_settings[content_type]["voice_settings"]
                    settings = self.unrealspeech.map_voice_settings(elevenlabs_settings)
                    model = "default"  # UnrealSpeech doesn't have model selection
            
            # Check cache first - include voice settings in cache key
            cache_key = self._get_cache_key(normalized_text, voice_name, settings)
            logger.info(f"Cache key: {cache_key}")
            if cached_data := self._get_cached_audio(cache_key):
                logger.info(f"Using cached audio: {cached_data['path']}")
                return cached_data
            
            logger.info(f"Generating speech for text: '{normalized_text[:50]}...' using Daniel voice")
            
            # Generate unique filename
            timestamp = int(time.time())
            filename = f"{self.provider_name}_{timestamp}_Daniel.mp3"
            output_path = self.output_dir / filename
            
            # Generate speech using the appropriate provider
            if self.provider_name == "elevenlabs":
                result = self._generate_speech_elevenlabs(normalized_text, voice_name, settings, model, output_path)
            elif self.provider_name == "unrealspeech":
                result = self._generate_speech_unrealspeech(normalized_text, voice_name, settings, output_path)
            
            # Update cache
            self._cache_audio(cache_key, result)
            
            return result
            
        except Exception as e:
            logger.error(f"TTS generation failed: {str(e)}")
            raise TTSError(f"Failed to generate speech: {str(e)}")
    
    def _generate_speech_elevenlabs(self, text: str, voice_name: str, settings: Dict, model: str, output_path: Path) -> Dict:
        """Generate speech using ElevenLabs API."""
        try:
            # Find the voice
            voice = None
            for v in self.available_voices:
                if v.name.lower() == voice_name.lower():
                    voice = v
                    break
            
            if not voice:
                raise TTSError(f"Voice '{voice_name}' not found in ElevenLabs voices")
            
            voice_id = voice.voice_id
            
            # Handle test mode
            if "test" in self.api_key.lower() or voice_id.startswith("test_"):
                logger.warning("Using test mode for ElevenLabs API")
                
                # Create a test audio file (sine wave)
                test_audio = Sine(440).to_audio_segment(duration=2000)  # 2 seconds
                # Add another second with a different tone
                test_audio = test_audio + Sine(880).to_audio_segment(duration=3000)  # 3 more seconds
                
                # Export to the specified path
                test_audio.export(output_path, format="mp3")
                
                # Create approximate word timings based on text length
                approximate_timings = self._generate_approximate_word_timings(text, len(test_audio) / 1000.0)
                # For approximate timings, expand numeric references to improve highlighting
                word_timings = self._expand_numeric_references(approximate_timings)
                
                return {
                    "path": str(output_path),
                    "word_timings": word_timings,
                    "duration": len(test_audio) / 1000.0  # Convert ms to seconds
                }
            else:
                logger.info("Generating speech with ElevenLabs API")
                
                # Get audio generator from the API
                audio_generator = self.client.text_to_speech.convert(
                    text=text,
                    voice_id=voice_id,
                    model_id=model,
                    voice_settings=settings,
                    output_format="mp3_44100_128"
                )
                
                # Save audio file by collecting chunks from the generator
                with open(output_path, "wb") as f:
                    # If it's a generator, collect chunks
                    if hasattr(audio_generator, '__iter__') and not isinstance(audio_generator, bytes):
                        for chunk in audio_generator:
                            f.write(chunk)
                    else:
                        # If it's already bytes, write directly
                        f.write(audio_generator)
                
                # Generate approximate word timings based on audio duration
                try:
                    # AudioSegment already imported at the top of the file
                    audio_segment = AudioSegment.from_file(output_path)
                    duration = len(audio_segment) / 1000.0  # Convert ms to seconds
                except Exception as e:
                    logger.warning(f"Error measuring audio duration: {e}")
                    # Estimate duration based on text length as fallback
                    duration = len(text) / 15  # Rough estimate: 15 chars per second
                
                # Generate approximate word timings
                approximate_timings = self._generate_approximate_word_timings(text, duration)
                # For approximate timings, expand numeric references to improve highlighting
                word_timings = self._expand_numeric_references(approximate_timings)
                
                # Fix Quranic references ordering issue
                word_timings = self._fix_reference_order(text, word_timings)
                
                return {
                    "path": str(output_path),
                    "word_timings": word_timings,
                    "duration": duration
                }
                
        except Exception as e:
            raise TTSError(f"Failed to generate speech with ElevenLabs: {str(e)}")

    def _fix_reference_order(self, original_text: str, word_timings: List[Tuple[str, float, float]]) -> List[Tuple[str, float, float]]:
        """
        Fix the order of parenthetical Quranic references in word timings.
        ElevenLabs sometimes places references like "(Al-A'raf 7:28)" at the beginning
        of the word timings, even though they appear elsewhere in the text.
        
        Args:
            original_text: The original text sent to the TTS API
            word_timings: List of (word, start_time, end_time) tuples
            
        Returns:
            Reordered list of word timings
        """
        # Quick check if reordering is needed - look for parenthetical references
        parenthetical_refs = re.findall(r'\([^)]*\d+:\d+[^)]*\)', original_text)
        if not parenthetical_refs:
            return word_timings  # No references to fix
        
        # Extract just the words from the timings for easier analysis
        words = [w[0] for w in word_timings]
        joined_words = " ".join(words)
        
        # Check if we have a reference at the start that shouldn't be there
        for ref in parenthetical_refs:
            # If reference appears in the first 3 words but not at the beginning of original text
            ref_parts = ref.split()
            if any(word.startswith(ref_parts[0].strip('(')) for word in words[:3]) and not original_text.strip().startswith(ref_parts[0]):
                logger.warning(f"Detected misplaced reference: {ref}")
                
                # Find where this reference should be in the original text
                try:
                    # Get the approximate position in the original text
                    ref_pos_in_text = original_text.find(ref)
                    if ref_pos_in_text > 0:
                        # Count words before this position (rough estimate)
                        words_before_ref = original_text[:ref_pos_in_text].split()
                        target_position = len(words_before_ref)
                        
                        # Find the reference in our word timings
                        ref_indices = []
                        ref_start = -1
                        ref_end = -1
                        
                        # Look for parts of the reference in the words
                        for i, word in enumerate(words):
                            if i > 0 and ref_start == -1 and word.startswith(ref_parts[0].strip('(')):
                                ref_start = i
                            if ref_start != -1 and ref_end == -1:
                                ref_indices.append(i)
                                # Check if we've found the last part of the reference
                                if word.endswith(')') or ')' in word:
                                    ref_end = i
                                    break
                        
                        # If we found the reference, move it to the correct position
                        if ref_start != -1 and ref_end != -1:
                            logger.info(f"Moving reference from positions {ref_start}-{ref_end} to around position {target_position}")
                            
                            # Extract the reference timing entries
                            ref_timings = word_timings[ref_start:ref_end+1]
                            
                            # Remove them from their current position
                            new_timings = word_timings[:ref_start] + word_timings[ref_end+1:]
                            
                            # Clamp target position to valid range in the new list
                            target_position = min(target_position, len(new_timings))
                            
                            # Insert at the target position
                            reordered_timings = new_timings[:target_position] + ref_timings + new_timings[target_position:]
                            
                            return reordered_timings
                except Exception as e:
                    logger.error(f"Error reordering references: {e}")
        
        # Return original timings if we couldn't fix the ordering
        return word_timings
    
    def _generate_speech_unrealspeech(self, text: str, voice_name: str, settings: Dict, output_path: Path) -> Dict:
        """Generate speech using UnrealSpeech provider."""
        try:
            # Use the UnrealSpeech provider to generate speech
            result = self.unrealspeech.generate_speech(
                text=text,
                voice_name=voice_name,
                output_dir=self.output_dir,
                settings=settings
            )
            
            return result
            
        except UnrealSpeechError as e:
            raise TTSError(f"Failed to generate speech with UnrealSpeech: {str(e)}")
            
    def _generate_approximate_word_timings(self, text: str, duration: float) -> List[Tuple[str, float, float]]:
        """
        Generate approximate word timings based on text length and audio duration.
        This is a fallback when API-provided timestamps are not available.
        
        Args:
            text: The text that was converted to speech
            duration: The duration of the audio in seconds
            
        Returns:
            List of (word, start_time, end_time) tuples
        """
        words = text.split()
        if not words:
            return []
            
        # Define patterns for special cases that need more time
        verse_reference_pattern = re.compile(r'^\d+:\d+(-\d+)?$')  # Matches patterns like 99:7 or 99:7-8
        numeric_pattern = re.compile(r'^\d+(\.\d+)?$')  # Matches numbers like 99, 99.7
        
        # Calculate average word duration
        avg_word_duration = duration / len(words)
        
        # First pass - calculate initial durations and identify special cases
        initial_durations = []
        special_case_indices = []
        
        for i, word in enumerate(words):
            # Check for verse references (like 99:7-8)
            if verse_reference_pattern.match(word):
                # Religious verse references need significantly more time
                # They're typically spoken as "ninety-nine, verses seven to eight"
                base_factor = len(word) / 5
                adjusted_factor = base_factor * 3.0  # 3x longer than text length suggests
                word_length_factor = max(0.5, min(4.0, adjusted_factor))
                special_case_indices.append(i)
            # Check for standalone numbers (like 99)
            elif numeric_pattern.match(word):
                # Numbers are often spoken as full words ("ninety-nine")
                base_factor = len(word) / 5
                adjusted_factor = base_factor * 2.0  # 2x longer than text length suggests
                word_length_factor = max(0.5, min(3.0, adjusted_factor))
                special_case_indices.append(i)
            else:
                # Regular word timing
                word_length_factor = len(word) / 5  # Assuming average word length is 5 characters
                word_length_factor = max(0.5, min(2.0, word_length_factor))  # Limit between 0.5x and 2x
            
            initial_durations.append(word_length_factor * avg_word_duration)
        
        # Adjust for the extra time given to special cases
        if special_case_indices and len(words) > len(special_case_indices):
            # Calculate how much extra time special cases are taking
            total_duration = sum(initial_durations)
            
            # If special cases take too much time, slightly reduce regular word timing
            if total_duration > duration:
                # Calculate scale factor to fit within total duration
                scale_factor = duration / total_duration
                
                # Apply scale factor to all durations
                initial_durations = [d * scale_factor for d in initial_durations]
        
        # Generate word timings based on calculated durations
        word_timings = []
        current_time = 0.0
        
        for i, word in enumerate(words):
            word_duration = initial_durations[i]
            word_timings.append((word, current_time, current_time + word_duration))
            current_time += word_duration
        
        # Finally normalize to match total audio duration
        if word_timings:
            last_word_end = word_timings[-1][2]
            if abs(last_word_end - duration) > 0.01:  # Allow small tolerance
                scale_factor = duration / last_word_end
                word_timings = [(word, start * scale_factor, end * scale_factor) 
                               for word, start, end in word_timings]
        
        # No longer expanding numeric references here - this is now done conditionally by the caller
        return word_timings

    def _expand_numeric_references(self, word_timings: List[Tuple[str, float, float]]) -> List[Tuple[str, float, float]]:
        """
        Expand numeric references into multiple tokens with their own timings for better karaoke highlighting.
        Uses a flexible approach that doesn't make assumptions about specific words being spoken.
        
        Args:
            word_timings: List of (word, start_time, end_time) tuples
            
        Returns:
            List of expanded (word, start_time, end_time) tuples
        """
        expanded_timings = []
        verse_reference_pattern = re.compile(r'^\d+:\d+(-\d+)?$')  # Matches patterns like 99:7 or 99:7-8
        numeric_pattern = re.compile(r'^\d+(\.\d+)?$')  # Matches numbers like 99, 99.7
        citation_pattern = re.compile(r'\([^)]*\d+:\d+[^)]*\)')  # Matches citation patterns like (Al-A'raf 7:28)
        
        # First pass: identify and fix citation blocks
        i = 0
        while i < len(word_timings):
            word, start_time, end_time = word_timings[i]
            
            # Check if this word is part of a citation
            if '(' in word:
                citation_parts = []
                citation_start = start_time
                citation_end = end_time
                j = i
                
                # Collect all parts of the citation
                while j < len(word_timings):
                    current_word, curr_start, curr_end = word_timings[j]
                    citation_parts.append((current_word, curr_start, curr_end))
                    citation_end = curr_end
                    
                    if ')' in current_word:
                        break
                    j += 1
                
                # If we found a complete citation
                if j < len(word_timings) and citation_parts:
                    citation_text = ' '.join(part[0] for part in citation_parts)
                    if citation_pattern.search(citation_text):
                        # Add the complete citation as one unit
                        expanded_timings.append((citation_text, citation_start, citation_end))
                        i = j + 1
                        continue
            
            # Handle verse references and numbers
            if verse_reference_pattern.match(word):
                duration = end_time - start_time
                parts = word.split(':')
                
                if '-' in parts[1]:  # Format like "99:7-8"
                    chapter = parts[0]
                    verse_range = parts[1]
                    chapter_duration = duration * 0.6
                    expanded_timings.append((chapter, start_time, start_time + chapter_duration))
                    expanded_timings.append((verse_range, start_time + chapter_duration, end_time))
                else:  # Format like "99:7"
                    chapter = parts[0]
                    verse = parts[1]
                    chapter_duration = duration * 0.6
                    expanded_timings.append((chapter, start_time, start_time + chapter_duration))
                    expanded_timings.append((verse, start_time + chapter_duration, end_time))
                
            elif numeric_pattern.match(word) and len(word) > 1:
                # Handle numeric values (e.g., "99" -> "ninety" "nine")
                duration = end_time - start_time
                num = int(float(word))
                
                if 20 < num < 100:
                    tens_digit = num // 10
                    ones_digit = num % 10
                    first_duration = duration * 0.6
                    
                    expanded_timings.append((f"{tens_digit}0", start_time, start_time + first_duration))
                    if ones_digit > 0:
                        expanded_timings.append((f"{ones_digit}", start_time + first_duration, end_time))
                    else:
                        expanded_timings[-1] = (expanded_timings[-1][0], expanded_timings[-1][1], end_time)
                else:
                    expanded_timings.append((word, start_time, end_time))
            else:
                expanded_timings.append((word, start_time, end_time))
            
            i += 1
        
        return expanded_timings
        
    def _cache_audio(self, cache_key: str, result: Dict):
        """Cache the generated audio file and word timings."""
        # Ensure duration is included in the cache
        if "duration" not in result:
            try:
                audio = AudioSegment.from_file(result["path"])
                result["duration"] = len(audio) / 1000.0  # Convert ms to seconds
            except Exception as e:
                logger.warning(f"Failed to get audio duration for cache: {e}")
                result["duration"] = 0.0  # Default to 0 if we can't get the duration
        
        self.cache[cache_key] = {
            "path": result["path"],
            "word_timings": result.get("word_timings", []),
            "duration": result.get("duration", 0.0)
        }
        self._save_cache() 
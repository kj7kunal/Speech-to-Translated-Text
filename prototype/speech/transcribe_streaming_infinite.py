"""Application for transcribing and translating
    recorded Japanese speech to English
Author: Kunal Jain

NOTE: This module requires the dependency `pyaudio`,
     `google-cloud-translate`, `google-cloud-speech`

To install using pip:

    pip install -r requirements.txt

Example usage:
    python transcribe_streaming_infinite.py
"""

import time
import re
import sys

from google.cloud import speech_v1 as speech
from google.cloud import translate_v2 as translate
from google.api_core.exceptions import DeadlineExceeded
import pyaudio
import wave
from six.moves import queue

# Audio recording parameters
STREAMING_LIMIT = 10000
SAMPLE_RATE = 16000
CHUNK_SIZE = int(SAMPLE_RATE / 10)  # 100ms

RED = '\033[0;31m'
GREEN = '\033[0;32m'
YELLOW = '\033[0;33m'


def get_current_time():
    """Return Current Time in MS."""
    return int(round(time.time() * 1000))


class ResumableMicrophoneStream:
    """Opens a recording stream as a generator yielding the audio chunks."""

    def __init__(self, rate, chunk_size):
        self._rate = rate
        self.chunk_size = chunk_size
        self._num_channels = 1
        self._buff = queue.Queue()
        self.closed = True
        self.start_time = get_current_time()
        self.restart_counter = 0
        self.audio_input = []
        self.last_audio_input = []
        self.result_end_time = 0
        self.is_final_end_time = 0
        self.final_request_end_time = 0
        self.bridging_offset = 0
        self.last_transcript_was_final = False
        self.new_stream = True
        self._audio_interface = pyaudio.PyAudio()
        self._audio_stream = self._audio_interface.open(
            format=pyaudio.paInt16,
            channels=self._num_channels,
            rate=self._rate,
            input=True,
            frames_per_buffer=self.chunk_size,
            # Run the audio stream asynchronously to fill the buffer object.
            # This is necessary so that the input device's buffer doesn't
            # overflow while the calling thread makes network requests, etc.
            stream_callback=self._fill_buffer,
        )

    def __enter__(self):

        self.closed = False
        return self

    def __exit__(self, *args):

        self._audio_stream.stop_stream()
        self._audio_stream.close()
        self.closed = True
        # Signal the generator to terminate so that the client's
        # streaming_recognize method will not block the process termination.
        self._buff.put(None)
        self._audio_interface.terminate()
        sys.stdout.write(YELLOW)
        sys.stdout.write('Exiting...\n')

    def _fill_buffer(self, in_data, *args, **kwargs):
        """Continuously collect data from the audio stream, into the buffer."""

        self._buff.put(in_data)
        return None, pyaudio.paContinue

    def generator(self):
        """Stream Audio from microphone to API and to local buffer"""

        while not self.closed:
            data = []

            if self.new_stream and self.last_audio_input:
                # if this is the first audio from a new request
                # calculate amount of unfinalized [RED] audio from last request
                # resend the audio to the speech client before incoming audio
                # for continuation in speech
                # main bottleneck (unclear speech difficult to finalize)

                chunk_time = STREAMING_LIMIT / len(self.last_audio_input)

                if chunk_time != 0:
                    # last_audio_input > STREAMING_LIMIT => unfinalized ignored

                    if self.bridging_offset < 0:
                        # bridging Offset accounts for time of resent audio
                        # calculated from last request
                        self.bridging_offset = 0

                    if self.bridging_offset > self.final_request_end_time:
                        self.bridging_offset = self.final_request_end_time

                    # chunks from MS is number of chunks to resend
                    chunks_from_ms = round((self.final_request_end_time -
                                            self.bridging_offset) / chunk_time)

                    # set bridging offset for the next request
                    self.bridging_offset = (round((
                        len(self.last_audio_input) - chunks_from_ms)
                                                  * chunk_time))

                    for i in range(chunks_from_ms, len(self.last_audio_input)):
                        data.append(self.last_audio_input[i])

                self.new_stream = False

            # Use a blocking get() to ensure there's at least one chunk of
            # data, and stop iteration if the chunk is None, indicating the
            # end of the audio stream.
            chunk = self._buff.get()
            self.audio_input.append(chunk)

            if chunk is None:
                return
            data.append(chunk)

            # Now consume whatever other data's still buffered.
            while True:
                try:
                    chunk = self._buff.get(block=False)

                    if chunk is None:
                        return
                    data.append(chunk)

                    self.audio_input.append(chunk)

                except queue.Empty:
                    break

            yield b''.join(data)


def listen_print_loop(responses, translate_client, stream):
    """Iterates through server responses and prints them.

    The responses passed is a generator that will block until a response
    is provided by the server.

    Each response may contain multiple results, and each result may contain
    multiple alternatives; for details, see https://goo.gl/tjCPAU.  Here we
    print only the transcription for the top alternative of the top result.

    No result may exist if speech could not be recognized. Unrecognized speech
    is commonly the result of very poor-quality audio, or from language code,
    encoding, or sample rate values that do not match the supplied audio.

    In this case, responses are provided for interim results as well. If the
    response is an interim one, print a line feed at the end of it, to allow
    the next result to overwrite it, until the response is a final one. For the
    final one, print a newline to preserve the finalized transcription.
    """

    all_transcripts = []
    all_final_transcripts = []

    for response in responses:

        if get_current_time() - stream.start_time > STREAMING_LIMIT:
            stream.start_time = get_current_time()
            break

        # look for results in other responses
        if not response.results:
            continue

        # The `results` list is consecutive. For streaming, we only care about
        # the first result being considered, since once it's `is_final`, it
        # moves on to considering the next utterance.
        result = response.results[0]

        # check next response if possible transcriptions don't exist
        if not result.alternatives:
            continue

        # choose the highest confidence transcript from alternatives
        transcript = result.alternatives[0].transcript

        # get translation of transcript
        translated = translate_client.translate(transcript, source_language="ja")

        result_seconds = 0
        result_nanos = 0

        if result.result_end_time.seconds:
            result_seconds = result.result_end_time.seconds

        if result.result_end_time.nanos:
            result_nanos = result.result_end_time.nanos

        # get result_end_time in ms for corrected time stamp
        stream.result_end_time = int((result_seconds * 1000)
                                     + (result_nanos / 1000000))

        corrected_time = (stream.result_end_time - stream.bridging_offset
                          + (STREAMING_LIMIT * stream.restart_counter))

        if result.is_final:
            # High confidence transcription from model
            stream.is_final_end_time = stream.result_end_time
            stream.last_transcript_was_final = True
            all_final_transcripts.append(str(corrected_time) + ': ' +
                                         translated['input'] + '\n' +
                                         translated['translatedText'] + '\n')
        else:
            # Low confidence transcription from model
            stream.last_transcript_was_final = False
            all_transcripts.append(str(corrected_time) + ': ' +
                                   translated['input'] + '\n' +
                                   translated['translatedText'] + '\n')

    if all_final_transcripts:
        # print all_final_transcripts[-1] with GREEN
        sys.stdout.write(GREEN)
        sys.stdout.write(all_final_transcripts[-1])
        all_final_transcripts = []
    elif all_transcripts:
        # print all_transcripts[-1] with RED
        sys.stdout.write(RED)
        sys.stdout.write(all_transcripts[-1])
        all_transcripts = []


def audio_saver(data, sampw, num):
    """Saves the recorded stream into wav files"""

    waveFile = wave.open('./recordings/record_' +
                         time.strftime("%Y%m%d-%H%M%S")+'_'+num+'.wav', 'wb')
    waveFile.setnchannels(1)
    waveFile.setsampwidth(sampw)
    waveFile.setframerate(16000)
    waveFile.writeframes(b''.join(data))
    waveFile.close()


def main():
    """start bidirectional streaming from microphone input to speech API"""

    program_run_time = time.time()

    speech_client = speech.SpeechClient()
    translate_client = translate.Client(target_language='en')

    config = speech.types.RecognitionConfig(
        encoding=speech.enums.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=SAMPLE_RATE,
        language_code='ja-JP',
        model='default',
        max_alternatives=1)
    streaming_config = speech.types.StreamingRecognitionConfig(
        config=config,
        interim_results=True)

    mic_manager = ResumableMicrophoneStream(SAMPLE_RATE, CHUNK_SIZE)

    sys.stdout.write(YELLOW)
    sys.stdout.write('\nListening, say "Quit" or "Exit" to stop.\n\n')
    sys.stdout.write('End (ms)       Transcript Results/Status\n')
    sys.stdout.write('=====================================================\n')

    with mic_manager as stream:

        while not stream.closed:
            sys.stdout.write(YELLOW)
            sys.stdout.write('\n' + str(
                STREAMING_LIMIT * stream.restart_counter) + ': NEW REQUEST\n')

            # Save the last processed audio input
            if stream.last_audio_input:
                audio_saver(stream.last_audio_input,
                            stream._audio_interface.get_sample_size(pyaudio.paInt16),
                            str(STREAMING_LIMIT * stream.restart_counter))

            # Initialize current input data to be processed
            stream.audio_input = []

            # Call generator method to get current audio buffer data
            audio_generator = stream.generator()

            # Create requests structure from captured streams
            requests = (speech.types.StreamingRecognizeRequest(
                audio_content=content)for content in audio_generator)

            # Get responses from Streaming Recognition client
            responses = speech_client.streaming_recognize(streaming_config,
                                                          requests)

            # Now, get the transcript and translation from responses
            try:
                listen_print_loop(responses, translate_client, stream)
            except DeadlineExceeded:
                print("Deadline_exceeded")
                continue
            except KeyboardInterrupt:
                stream.__exit__()
                break

            # Reset stream for next iteration
            if stream.result_end_time > 0:
                stream.final_request_end_time = stream.is_final_end_time
            stream.result_end_time = 0
            stream.last_audio_input = []
            stream.last_audio_input = stream.audio_input
            stream.audio_input = []
            stream.restart_counter = stream.restart_counter + 1

            if not stream.last_transcript_was_final:
                sys.stdout.write('\n')
            stream.new_stream = True

    sys.stdout.write("STTT used for: " + str(time.time()-program_run_time) +
                     'seconds \n')


if __name__ == '__main__':
    main()

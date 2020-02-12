# Prototype for Speech-to-Translated-Text (STTT)

Speech Translation Prototype using python, Google Speech Recognition API and
Google Translation API

## Installation

Using pip: 
```
pip install -r requirements.txt
```
Requirements: 
- google-cloud-translate==2.0.0
- google-cloud-speech==1.3.1
- pyaudio==0.2.11
- six==1.13.0

If a problem with pyaudio installation exists, try installing manually
```
brew update
brew install portaudio
brew link --overwrite portaudio
pip install pyaudio
pip install --global-option='build_ext' --global-option='-I/usr/local/include' --global-option='-L/usr/local/lib' pyaudio
```

The script also requires that you have set up gcloud and have created and 
activated a service account: 
[Setup Instructions](https://cloud.google.com/speech-to-text/docs/quickstart-client-libraries)

## Usage

1. Set the environment variable $GOOGLE_APPLICATION_CREDENTIALS to the path of 
the JSON file that contains your service account key
```
export GOOGLE_APPLICATION_CREDENTIALS="[PATH]"
```

2. Make directory for storing recordings in the project directory
```
cd prototype/speech/
mkdir recordings
```

3. Run STTT using:
```
python transcribe_streaming_infinite.py
```

## Flow

### Microphone streaming
Class `ResumableMicrophoneStream` defines the stream object to collect audio 
using python's pyaudio library, which provides Python bindings for PortAudio, 
the cross-platform audio I/O library. 

- The `_audio_interface` variable is initialized with a pyaudio.PyAudio class 
object which sets up the portaudio system.
- A stream is setup on the default recording device using the `open()` method 
with `stream_callback` set to asynchronously fill a local buffer object of 
the `Queue` library using `_fill_buffer` method which passes the 
`pyaudio.paContinue` flag for continuous capture. This is necessary so that the 
input device's buffer doesn't overflow while the calling thread makes requests.
- Other parameters initializing the stream object are a `sampling_rate` of 
16kHz and a `frames_per_buffer` of 1600
- The `generator` method is used to stream audio from microphone to APIs and 
to the local buffer:
    - In case the audio is from a new request, calculate amount of unfinalized 
    audio from last request and resend the audio to the speech client before 
    incoming audio for continuation in speech
    - `STREAMING_LIMIT` determines the amount of unfinalized audio to be 
    included in the new request
    - Get at least one new chunk from the buffer using `Queue.get(block=True)` 
    or exit loop if there are no new chunks
    - Get all the other new chunks using `Queue.get(block=False)`, which gets 
    data if immediately available or raises `queue.Empty` exception
    - Return generated data for the current processing iteration    
- During run, `audio_input` class variable contains audio input for current 
processing step and `last_audio_input` contains the previous request audio used 
for continuation in transcription
- While exiting the program, the `__exit__()` method of the class is invoked 
to stop pyaudio processes using `stop_stream()` and clear out the input device 
buffer using `close()` and finally terminate PortAudio using `terminate()`
- The local buffer object is also set to None while exiting

### Recording
Python's wave library is used to store the audio data from `last_audio_input`
into separate files numbered by the current stream counter, with the same 
sampling rate and sampling width defined for the pyaudio recording stream.
`pyaudio.paInt16` or 16-bit sampling format is used.

### Google Speech-to-Text API
```
from google.cloud import speech_v1 as speech
```

The Google Speech-to-Text API requires the user to instantiate a `SpeechClient()` 
object which authenticates the service account access to the API using the 
credential JSON file downloaded during setup. $GOOGLE_APPLICATION_CREDENTIALS 
needs to be set to the file's path.

The `RecognitionConfig` and the `StreamingRecognitionConfig` class are configured
for parameters:
- encoding = speech.enums.RecognitionConfig.AudioEncoding.LINEAR16
- sample_rate_hertz = SAMPLE_RATE (16000)
- language_code='ja-JP' (source language - no detection)
- model='default' (model for long-form audio or dictation) 
[(source)](https://cloud.google.com/speech-to-text/docs/transcription-model)
- max_alternatives = 1 (Only the most confident transcription used)
- interim_results = True (Get not is_final transcriptions as well)

`StreamingRecognizeRequest` class is used to create messages requesting the 
audio data to be recognized

`streaming_recognize(config,requests)` method in the client object returns 
`StreamingRecognizeResponse` messages for the requests wrt the above streaming 
configuration: 
```
Example:

Interim Result:
results { alternatives { transcript: " that is" } stability: 0.9 } results { alternatives { transcript: " the question" } stability: 0.01 }

Final Result:
results { alternatives { transcript: " that is the question" confidence: 0.98 } alternatives { transcript: " that was the question" } is_final: true }
```
For streaming, the first alternative is taken from the first result of every 
response as transcript since the results list is consecutive and we only care 
about the first result being considered.
[(source)](https://github.com/googleapis/google-cloud-python/issues/3166)

Since `interim_results=True`, we print only the last result for each stream

### Google Text Translation API
```
from google.cloud import translate_v2 as translate
```
The Google Translation API is free, and does not require a service account 
access for using the API. 

The translate client object is instantiated with the `target_language` as 
`en` (English)

Translated text is obtained by calling the `translate()` method on the 
transcript with the `source_language` parameter being set to `ja` (Japanese), 
which returns a structure containing an `input` and a `translatedText` variable


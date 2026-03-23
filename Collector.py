import time
import pandas as pd
from pynput import keyboard

data = []
key_press_times = {}
last_release_time = None

def on_press(key):
    global last_release_time
    current_time = time.time()
    
    try:
        key_name = key.char
    except AttributeError:
        key_name = str(key)

    if key_name not in key_press_times:
        key_press_times[key_name] = current_time
        
        flight_time = 0
        if last_release_time is not None:
            flight_time = current_time - last_release_time
            
        return flight_time

def on_release(key):
    global last_release_time, data
    current_time = time.time()
    
    try:
        key_name = key.char
    except AttributeError:
        key_name = str(key)

    if key_name in key_press_times:
        press_time = key_press_times.pop(key_name)
        dwell_time = current_time - press_time
        
        data.append({'key': key_name, 'dwell_time': dwell_time})
        last_release_time = current_time

    if key == keyboard.Key.esc:
        df = pd.DataFrame(data)
        df.to_csv('user_typing_data.csv', index=False)
        print("\nData saved to user_typing_data.csv. Exiting...")
        return False

print("Recording... Type naturally. Press 'Esc' to stop.")
with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
    listener.join()
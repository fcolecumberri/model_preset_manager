import base64
import gradio as gr
import hashlib
import json
import numpy as np
import os
import re
import requests  # Replace urllib.request with requests
import subprocess
import time

from io import BytesIO
from modules import generation_parameters_copypaste as parameters_copypaste
from modules import script_callbacks
from modules import shared
from PIL import Image
from pathlib import Path
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from typing import Any


CHROMEDRIVER_DIRECTORY = os.path.join(os.path.dirname(os.path.realpath(__file__)), "chromedriver", "chromedriver.exe")
os.environ["PATH"] += os.pathsep + CHROMEDRIVER_DIRECTORY

IMG_CLASS = 'mantine-7aj0so'
MODEL_CLASS = "mantine-x8rksy"
TRIGGER_WORD_CONTAINER_XPATH = "//div[contains(@id, '-panel-version-details')]"
TRIGGER_WORD_SEARCH_PATTERN = r'<div class="mantine-Group-root mantine-i72d0e">\s*(.*?)<svg'

CIVITAI_URL_BASE = "https://civitai.com"


def get_model_info_file_path(model_hash):
    current_directory = os.path.dirname(__file__)
    return os.path.join(current_directory, "model presets",f"{model_hash}.json")

def empty_model_info():
    return  {
                "url": "",
                "default_preset" : "default",
                "trigger_words": [],
                "presets": {"default": ""}
            }

def initialize_model_info_file(model_hash):
    model_info_file_path = get_model_info_file_path(model_hash)
    
    os.makedirs(os.path.dirname(model_info_file_path), exist_ok=True)
    
    if not os.path.exists(model_info_file_path):
        with open(model_info_file_path, "w") as file:
            empty_model_info_file = empty_model_info()

            json.dump(empty_model_info_file, file, indent=4)
    return model_info_file_path

def get_model_hash_and_info_from_model_filename(model_filename, initializeIfMissing = True):    
    short_hash = get_short_hash_from_filename(model_filename)
    if initializeIfMissing:
        model_info_file_path = initialize_model_info_file(short_hash)
    else:
        model_info_file_path = get_model_info_file_path(short_hash)
    
    try:
        with open(model_info_file_path, "r") as file:
            return short_hash, json.load(file)
    except FileNotFoundError:
        return short_hash, empty_model_info()
        
def get_model_hash_and_info_from_current_model(initializeIfMissing = True):
    return get_model_hash_and_info_from_model_filename(current_model_filename(), initializeIfMissing)

def get_model_info_from_model_hash(model_hash):     
    model_info_file_path = initialize_model_info_file(model_hash)
    with open(model_info_file_path, "r") as file:
        return json.load(file)

def save_model_info(short_hash, model_info):
    model_info_file_path = initialize_model_info_file(short_hash)
    with open(model_info_file_path, "w") as file:
        json.dump(model_info, file, indent=4)

def get_model_url_and_hash_from_filename(model_filename):
    short_hash, model_info = get_model_hash_and_info_from_model_filename(model_filename)

    # If the hash is not present in the database, retrieve the URL and store it in the database under the hash
    model_url = model_info.get("url") or find_citivai_model_url_from_hash(short_hash) or None
    model_info['url'] = model_url
    
    global triggerWordChoices
    model_info["trigger_words"] = triggerWordChoices

    # Write the updated model_info JSON
    save_model_info(short_hash, model_info)

    # Return the model URL (or None if not found) and the short hash
    return model_url, short_hash

def get_html_with_selenium(url, matching_class, timeout=10, max_retries=3, sleep_time=2):
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--log-level=SEVERE")
    chrome_options.add_argument("--blink-settings=imagesEnabled=false")
    
    # prevent a ton of disregarded console warnings
    chrome_options.add_experimental_option("excludeSwitches", ["enable-logging"])
    
    chromedriver_directory = os.path.join(os.path.dirname(os.path.realpath(__file__)), '..', 'chromedriver')
    os.environ["PATH"] += os.pathsep + chromedriver_directory

    driver = webdriver.Chrome(options=chrome_options)
    driver.set_script_timeout(timeout)

    retries = 0
    element_found = False

    print(f"checking url: {url}")
    while retries < max_retries:
        driver.get(url)
        time.sleep(sleep_time)

        try:
            element = driver.find_element_by_class_name(matching_class)
            element_found = True
            break
        except NoSuchElementException:
            retries += 1
            print(f"Element not found, retrying... ({retries}/{max_retries})")

    if not element_found:
        print("Failed to find the element after maximum retries")

    html = driver.page_source
    driver.quit()
    return html
    
def get_trigger_words_with_selenium(url, timeout=10, max_retries=3, sleep_time=2):
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--log-level=SEVERE")
    chrome_options.add_argument("--blink-settings=imagesEnabled=false")

    chrome_options.add_experimental_option("excludeSwitches", ["enable-logging"])

    chromedriver_directory = os.path.join(os.path.dirname(os.path.realpath(__file__)), '..', 'chromedriver')
    os.environ["PATH"] += os.pathsep + chromedriver_directory

    driver = webdriver.Chrome(options=chrome_options)
    driver.set_script_timeout(timeout)

    retries = 0
    outer_element_found = False
    print(f"checking url: {url}")
    while retries < max_retries:
        driver.get(url)
        time.sleep(sleep_time)

        try:
            outer_element = driver.find_element_by_xpath(TRIGGER_WORD_CONTAINER_XPATH)
            if outer_element:
                outer_element_found = True
                break
        except NoSuchElementException:
            retries += 1
            print(f"Outer element not found, retrying... ({retries}/{max_retries})")

    if not outer_element_found:
        print("Failed to find the outer trigger word element after maximum retries")
            
        html = driver.page_source
        driver.quit()
        
        return html, []
        
        
    inner_html = outer_element.get_attribute("innerHTML")

    pattern = TRIGGER_WORD_SEARCH_PATTERN
    trigger_words = re.findall(pattern, inner_html, re.DOTALL)
    
    driver.quit()
    
    return trigger_words  # Return the list of inner elements

def get_short_hash_from_filename(filename):
    match = re.search(r'\[(.*?)\]', filename)
    if match:
        return match.group(1)
    filename = remove_hash_and_whitespace(filename)
    os.path.join("models", "Stable-diffusion", filename)
    sha256 = hashlib.sha256()
    with open(filename, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b''):
            sha256.update(chunk)
    return sha256.hexdigest()[:10]

def find_citivai_model_url_from_hash(short_hash):
    url = f'{CIVITAI_URL_BASE}/?query={short_hash}'
    search_results_page = get_html_with_selenium(url, MODEL_CLASS)
    return find_model_url_on_search_page(search_results_page)

def find_model_url_on_search_page(html):
    anchor_start = html.find(f'<a class="{MODEL_CLASS}"')

    if anchor_start == -1:
        return None

    href_start = html.find('href="', anchor_start)
    href_end = html.find('"', href_start + 6)

    if href_start != -1 and href_end != -1:
        return f'{CIVITAI_URL_BASE}{html[href_start + 6:href_end]}'

    return None


def extract_image_url(html, modelName, short_hash):
    img_start = html.find(f'<img class="{IMG_CLASS}"')

    if img_start == -1:
        return None

    src_start = html.find('src="', img_start)
    src_end = html.find('"', src_start + 5)

    if src_start != -1 and src_end != -1:
        image_url = html[src_start + 5:src_end]
        download_thumbnail(image_url, modelName)

        return image_url

    return None

def remove_hash_and_whitespace(s, remove_extension = False):
    # Remove any whitespace and hash surrounded by square brackets
    cleaned_string = re.sub(r'\s*\[.*?\]', '', s)
    
    # If remove_extension is set to True, remove the file extension as well
    if remove_extension:
        cleaned_string = re.sub(r'\.[^.]*$', '', cleaned_string)
    
    return cleaned_string

def get_thumbnail_path(modelName):
    return os.path.join("models", "Stable-diffusion", modelName + ".png")

def download_thumbnail(image_url, modelName):
    response = requests.get(image_url)
    response.raise_for_status()

    # Open the image using PIL and create a thumbnail with max size 300x300
    img = Image.open(BytesIO(response.content))
    img.thumbnail((300, 300))
    
    # Save the thumbnail
    thumbnail_path = get_thumbnail_path(modelName)
    img.save(thumbnail_path)
    
def save_thumbnail_from_np_array(current_model, image):
    if image is None:
        print("no image in np array")
        return
        
    current_model = remove_hash_and_whitespace(current_model, True)
    
    # Open the image and create a thumbnail with max size 300x300
    image = Image.fromarray(np.uint8(image))
    image.thumbnail((300, 300))
    
    # Save the thumbnail
    thumbnail_path = get_thumbnail_path(current_model)
    image.save(thumbnail_path)
    
def get_model_thumbnail(url, short_hash, local, modelName):
    thumbnail_path = get_thumbnail_path(modelName)

    if not local or not os.path.exists(thumbnail_path):
        html = get_html_with_selenium(url, IMG_CLASS)
        image_url = extract_image_url(html, modelName, short_hash)
        if image_url:
            download_thumbnail(image_url, modelName)

    if os.path.exists(thumbnail_path):
        return thumbnail_path
    else:
        print("no local model thumbnail found")
        return None

def update_default_preset(model_info):
    default_preset_name = model_info.get("default_preset", "default") or "default"
    presets = model_info.get("presets", {})

    if default_preset_name not in presets:
        if presets:
            first_preset_name = next(iter(presets.keys()))
            model_info["default_preset"] = first_preset_name
        else:
            model_info["default_preset"] = "default"
            model_info["presets"]["default"] = ""

    return model_info



def get_default_preset(model_info):
    default_preset_name = model_info.get("default_preset", "default")
    
    if default_preset_name == "" or default_preset_name is None:
        default_preset_name = "default"

    presets = model_info.get("presets", {})

    if default_preset_name in presets:
        return default_preset_name, presets[default_preset_name]
    elif len(presets) > 0:
        first_preset_name, first_preset_value = next(iter(presets.items()))
        return first_preset_name, first_preset_value
    else:
        return "default", ""


def download_model_info(current_generation_data):
    model_filename = current_model_filename()
    model_url, short_hash = get_model_url_and_hash_from_filename(model_filename)
    trigger_words = []
    if model_url:
        model_thumbnail = get_model_thumbnail(model_url, short_hash, False, remove_hash_and_whitespace(model_filename, True))
        trigger_words = get_trigger_words_with_selenium(model_url)
    
    model_info = get_model_info_from_model_hash(short_hash)    
    
    preset_name, current_generation_data = get_default_preset(model_info)
        
    presets = model_info.get("presets",{})
                  
    global triggerWordChoices
    triggerWordChoices = trigger_words
    set_trigger_words(model_filename)
    return model_filename, model_url, model_thumbnail, model_generation_data_update_return(current_generation_data, preset_name), gr.CheckboxGroup.update(choices = trigger_words), gr.Dropdown.update(choices = list(presets.keys()), value = preset_name), preset_name, short_hash

def model_generation_data_update_return(current_generation_data, preset_name):
    model_hash, model_info = get_model_hash_and_info_from_current_model()
    default_preset_name, preset_original_data = get_default_preset(model_info)
    default = default_preset_name == preset_name
    return gr.Textbox.update(label = model_generation_data_label_text(default), value = current_generation_data)

def current_model_filename():
    return shared.opts.data.get('sd_model_checkpoint', 'Not found')

def retrieve_model_info_from_disk(current_generation_data):
    model_filename = current_model_filename()

    short_hash, model_info = get_model_hash_and_info_from_model_filename(model_filename, False)

    if model_info:
        model_url = model_info['url']

        if model_url:
            model_thumbnail = get_model_thumbnail(model_url, short_hash, True, remove_hash_and_whitespace(model_filename, True))
            
            preset_name, current_generation_data = get_default_preset(model_info)
            presets = model_info.setdefault('presets', {"default": ""})
            trigger_words = model_info.setdefault('trigger_words', [])
                
            global triggerWordChoices
            triggerWordChoices = trigger_words
            return model_filename, model_url, model_thumbnail, model_generation_data_update_return(current_generation_data, preset_name), gr.CheckboxGroup.update(choices = trigger_words), gr.Dropdown.update(choices = list(presets.keys()), value = preset_name), preset_name, short_hash
        else:
            presets = model_info.setdefault('presets', {"default": ""})
            return download_model_info(current_generation_data)

    else:
        # Handle the case when the model is not found in the data structure
        presets = model_info.setdefault('presets', {"default": ""})
        return download_model_info(current_generation_data)

def set_model_info(model_filename, label, info):
    short_hash, model_info = get_model_hash_and_info_from_model_filename(model_filename)    
    
    model_info[label] = info
    
    save_model_info(short_hash, model_info)
    return f"{label} updated."

def set_model_url(current_model, model_url):
    return set_model_info(current_model, 'url', model_url)
    
def show_model_url(model_url):
    iframe_html = f'<iframe src="{model_url}" width="100%" height="1080" frameborder="0"></iframe>'
    return iframe_html

def set_trigger_words(current_model):
    global triggerWordChoices
    return set_model_info(current_model, 'trigger_words', triggerWordChoices)

def bind_buttons(buttons, source_text_component):
    for tabname, button in buttons.items():
        parameters_copypaste.register_paste_params_button(parameters_copypaste.ParamBinding(paste_button=button, tabname=tabname, source_text_component=source_text_component, source_image_component=None, source_tabname=None))

def getCheckedBoxesFromPrompt(prompt):
    global triggerWordChoices
    checked_boxes = [choice for choice in triggerWordChoices if choice in prompt]
    return checked_boxes

def adjustPromptToCheckBox(checkBoxChange: gr.SelectData, prompt):
    new_prompt = prompt    
    if checkBoxChange.selected and checkBoxChange.value not in new_prompt:
        new_prompt = f"{checkBoxChange.value} {new_prompt}"
    elif not checkBoxChange.selected and checkBoxChange.value in new_prompt:
        new_prompt = re.sub(f"{checkBoxChange.value} ?", "", new_prompt).strip()
    return new_prompt

def compare_lists(list_a, list_b):
    # Remove duplicates from list_a
    list_a_no_duplicates = list(set(list_a))

    # Check if the lengths of the lists are the same
    if len(list_a_no_duplicates) != len(list_b):
        return False

    # Sort the lists
    list_a_no_duplicates.sort()
    list_b.sort()

    # Check if the lists contain the same elements
    for i in range(len(list_a_no_duplicates)):
        if list_a_no_duplicates[i] != list_b[i]:
            return False

    return True

def model_generation_data_label_text(default=False):
    return f"Model Generation Data{' (default preset)' if default else ''}"
  
def handle_text_change(prompt):
    checked_boxes = getCheckedBoxesFromPrompt(prompt)
    return checked_boxes

def handle_checkbox_change(checkBoxChange: gr.SelectData, prompt):
    new_prompt = adjustPromptToCheckBox(checkBoxChange, prompt)
    return new_prompt

def save_preset(preset_name_textbox_value, model_generation_data):
    short_hash, model_info = get_model_hash_and_info_from_current_model()
    model_info['presets'][preset_name_textbox_value] = model_generation_data
    save_model_info(short_hash, model_info)
    return gr.Dropdown.update(choices = list(model_info['presets'].keys()), value = preset_name_textbox_value),  f"{preset_name_textbox_value} saved", model_generation_data_update_return(model_generation_data, preset_name_textbox_value)

def rename_preset(preset_dropdown_value, preset_name_textbox_value, model_generation_data):
    short_hash, model_info = get_model_hash_and_info_from_current_model()
    new_current_preset_name = preset_name_textbox_value

    # Check if the preset name is already the same
    if preset_dropdown_value == preset_name_textbox_value:
        message = f"Preset already named {preset_name_textbox_value}"        
    # Check if the new preset name already exists
    elif preset_name_textbox_value in model_info['presets'].keys():
        message = f"Preset name {preset_name_textbox_value} already exists"
        new_current_preset_name = preset_dropdown_value
    else:
        # Rename the preset by creating a new key with the same value and removing the old one
        model_info['presets'][preset_name_textbox_value] = model_info['presets'][preset_dropdown_value]
        del model_info['presets'][preset_dropdown_value]
        if model_info['default_preset'] == preset_dropdown_value:
            model_info['default_preset'] = preset_name_textbox_value
        message = f"Preset {preset_dropdown_value} renamed to {preset_name_textbox_value}"

    save_model_info(short_hash, model_info)
    return gr.Dropdown.update(choices = list(model_info['presets'].keys()), value = new_current_preset_name), message, model_generation_data_update_return(model_generation_data, preset_dropdown_value)

def delete_preset(preset_dropdown_value, model_generation_data):   
    short_hash, model_info = get_model_hash_and_info_from_current_model()
    del model_info['presets'][preset_dropdown_value]
    model_info = update_default_preset(model_info)
    save_model_info(short_hash, model_info)
    new_current_preset_name, model_generation_data = get_default_preset(model_info)
    formatted_dict = json.dumps(model_info, indent=4)
    return gr.Dropdown.update(choices = list(model_info['presets'].keys()), value = new_current_preset_name), new_current_preset_name, f"Preset {preset_dropdown_value} deleted", model_generation_data_update_return(model_generation_data, new_current_preset_name)

def update_current_preset(preset_dropdown_value):
    model_hash, model_info = get_model_hash_and_info_from_current_model()
    new_model_generation_data = model_info['presets'].get(preset_dropdown_value,"")
    return preset_dropdown_value, model_generation_data_update_return(new_model_generation_data, preset_dropdown_value)

def set_default_preset(preset_dropdown_value, model_generation_data):
    short_hash, model_info = get_model_hash_and_info_from_current_model()
    model_info['default_preset'] = preset_dropdown_value
    save_model_info(short_hash, model_info)
    return f"{preset_dropdown_value} set to default", model_generation_data_update_return(model_generation_data, preset_dropdown_value)

def reveal_presets_file_in_explorer(model_hash):
    if not model_hash:
        return "no presets file for this model or no model retrieved"
        
    model_info_file_path = initialize_model_info_file(model_hash)

    explorer_path = os.path.join(os.environ["WINDIR"], "explorer.exe")
    explorer_command = f'"{explorer_path}" /e,/select,"{model_info_file_path}"'
    subprocess.run(explorer_command, shell=True)

def get_template_generation_data(includeExamplePrompt):
    prompt = "{Your Prompt Here}\n" if includeExamplePrompt else ""
    return (prompt + """Negative prompt:
Steps: 20, Sampler: Euler a, CFG scale: 7, Size: 512x512, Clip skip: 1
""")

def append_template_generation_info(generation_data):
    return generation_data + get_template_generation_data(generation_data == "")

triggerWordChoices = None
def on_ui_tabs():
    with gr.Blocks() as custom_tab_interface:
        current_model_textbox = gr.Textbox(interactive=False, label="Current Model:", visible=False) 
        with gr.Row():
            with gr.Column(scale = 1):
                pass  

            with gr.Column(min_width=1000, scale = 3):
                
                with gr.Column():
                    gr.Markdown('<center><h3>Model Info</h2></center>')
                    with gr.Box():
                        with gr.Row():
                            with gr.Column(min_width=300, scale = 1):
                                image_input = gr.Image(source="upload", width=300, height=300)
                            with gr.Column(scale = 4):
                                with gr.Row():
                                    with gr.Column(scale = 4):
                                        model_url_textbox = gr.Textbox(label="Model URL", scale = 40)
                                    with gr.Column(min_width=100, scale = 0.1):
                                        model_hash_textbox = gr.Textbox(label="Model Hash", interactive = False)  
                                    show_presets_in_explorer_button = gr.Button("Reveal Presets File")
                                with gr.Row():
                                    retrieve_button = gr.Button("Retrieve Local Model Info", elem_id = "retrieve_model_info_button")
                                    download_button = gr.Button("Download and Overwrite Model Info")
                                with gr.Row():
                                    open_model_page_button = gr.Button("Open Model Page")
                                    set_model_url_button = gr.Button("Set Model URL") 

                                                  
                with gr.Row():
                    with gr.Column():                    
                        gr.Markdown('<center><h3>Generation Data</h2></center>')
                        with gr.Box():
                            with gr.Row():
                                with gr.Column(scale = 6): 
                                    model_generation_data = gr.Textbox(label = model_generation_data_label_text(), value = "", lines = 3, elem_id = "def_model_gen_data_textbox").style(show_copy_button=True)       
                                append_template_button = gr.Button("Append Template")
                                
                            triggerWords = gr.CheckboxGroup([], multiselect=True, label="Trigger Words", interactive = True).style(container=True, item_container=True)
                            with gr.Row():
                                gr.Markdown('<div style="height: 10px;"></div>')
                            with gr.Row():
                                buttons = parameters_copypaste.create_buttons(["txt2img","img2img", "inpaint"])  
                    
                    with gr.Column():                  
                        gr.Markdown('<center><h3>Model Presets</h2></center>')
                        with gr.Box():
                            with gr.Row():
                                with gr.Column():
                                    preset_dropdown = gr.Dropdown(choices=[], label="Presets") 
                                    preset_name_textbox = gr.Textbox(label="Current Preset Name")
                                
                                with gr.Box():
                                    with gr.Row():
                                        gr.Markdown('<div style="height: 10px;"></div>')
                                    with gr.Row():
                                        save_preset_button = gr.Button("Save Preset")
                                        set_preset_button = gr.Button("Set Preset as Default") 
                                        delete_preset_button = gr.Button("Delete Preset")   
                                        rename_preset_button = gr.Button("Rename Preset")
                                    with gr.Row():
                                        gr.Markdown('<div style="height: 10px;"></div>')
                         
                with gr.Row():
                    output_textbox = gr.Textbox(interactive=False, label="Output")
                    with gr.Column():
                        pass
              
            with gr.Column(scale = 1):
                pass
            
        

        # Update the preset name textbox when a preset is selected in the dropdown
        preset_dropdown.change(fn=update_current_preset, inputs=[preset_dropdown], outputs=[preset_name_textbox, model_generation_data], show_progress=False)
                        
       
        model_url_output = gr.HTML(label="model page", height=800)  
   
        image_input.change(fn=save_thumbnail_from_np_array, inputs=[current_model_textbox, image_input])
        triggerWords.select(fn=handle_checkbox_change, inputs =[model_generation_data], outputs=[model_generation_data], show_progress=False)
        triggerWords.loading_html = ""            
                   
        open_model_page_button.click(fn=show_model_url, inputs=[model_url_textbox], outputs=[model_url_output])  
        set_model_url_button.click(fn=set_model_url, inputs=[current_model_textbox, model_url_textbox], outputs=[output_textbox]) 
        append_template_button.click(fn=append_template_generation_info, inputs=[model_generation_data], outputs=[model_generation_data]) 
        
        download_button.click(fn=download_model_info, inputs=[], outputs=[current_model_textbox, model_url_textbox, image_input, model_generation_data, triggerWords, preset_dropdown, preset_name_textbox, model_hash_textbox])
        retrieve_button.click(fn=retrieve_model_info_from_disk, inputs=[], outputs=[current_model_textbox, model_url_textbox, image_input, model_generation_data, triggerWords, preset_dropdown, preset_name_textbox, model_hash_textbox])
        show_presets_in_explorer_button.click(fn = reveal_presets_file_in_explorer, inputs = [model_hash_textbox], outputs = [output_textbox])
                
        set_preset_button.click(fn=set_default_preset, inputs=[preset_dropdown, model_generation_data], outputs=[output_textbox, model_generation_data ])                
        save_preset_button.click(fn=save_preset, inputs=[preset_name_textbox, model_generation_data], outputs=[preset_dropdown, output_textbox, model_generation_data])            
        rename_preset_button.click(fn=rename_preset, inputs=[preset_dropdown, preset_name_textbox, model_generation_data], outputs=[preset_dropdown, output_textbox, model_generation_data])
        delete_preset_button.click(fn=delete_preset, inputs=[preset_dropdown, model_generation_data], outputs=[preset_dropdown, preset_name_textbox, output_textbox, model_generation_data])         
        
        model_generation_data.change(fn = handle_text_change, inputs = [model_generation_data], outputs = [triggerWords], show_progress=False)
        
        bind_buttons(buttons, model_generation_data)       
        

    return [(custom_tab_interface, "Model Preset Manager", "model preset manager")]


script_callbacks.on_ui_tabs(on_ui_tabs)

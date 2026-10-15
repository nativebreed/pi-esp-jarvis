#!/usr/bin/env python3
"""Customise the xiaozhi-esp32 source tree before building (runs inside the builder).

    patch_firmware.py <board_name> <ota_url> [emoji_dir]

* points the firmware at the Pi (OTA URL) and plays a chime after the wake word
* uses the "big face" screen layout on a black background
* packs our animated face GIFs (one per mood) into the firmware
* double-tap the screen to start talking (or to interrupt the answer)
"""
import json
import re
import sys
from pathlib import Path

board, ota_url = sys.argv[1], sys.argv[2]
emoji_dir = Path(sys.argv[3]) if len(sys.argv) > 3 else None
root = Path(".")
board_dir = root / "main" / "boards" / "waveshare" / board


def patch(path: Path, old: str, new: str, count: int = 1) -> None:
    text = path.read_text(encoding="utf-8")
    if new in text:
        return  # already patched
    if text.count(old) < 1:
        sys.exit(f"patch_firmware: could not find expected code in {path}:\n{old}")
    path.write_text(text.replace(old, new, count), encoding="utf-8")


# --- 1. build options ---------------------------------------------------------
cfg_path = board_dir / "config.json"
cfg = json.loads(cfg_path.read_text())
extra = [
    f'CONFIG_OTA_URL="{ota_url}"',
    "CONFIG_SEND_WAKE_WORD_DATA=n",
    "CONFIG_LWIP_DNS_SUPPORT_MDNS_QUERIES=y",
    "CONFIG_USE_WECHAT_MESSAGE_STYLE=n",
    "CONFIG_USE_DEFAULT_MESSAGE_STYLE=y",
]
keys = {e.split("=")[0] for e in extra}
for build in cfg["builds"]:
    keep = [o for o in build.get("sdkconfig_append", []) if o.split("=")[0] not in keys]
    build["sdkconfig_append"] = keep + extra
cfg_path.write_text(json.dumps(cfg, indent=4))
print("patched: build options (server URL, big-face layout)")

# --- 2. black background by default -------------------------------------------
patch(root / "main/display/lcd_display.cc",
      'settings.GetString("theme", "light")', 'settings.GetString("theme", "dark")')
print("patched: dark theme by default")

# --- 3. our animated face -------------------------------------------------------
if emoji_dir and any(emoji_dir.glob("*.gif")):
    cmake = root / "main/CMakeLists.txt"
    text = cmake.read_text(encoding="utf-8")
    block = re.compile(
        r"(elseif\(CONFIG_BOARD_TYPE_WAVESHARE_ESP32_S3_TOUCH_AMOLED_1_8(?:_v2)?\)\n"
        r"(?:    .*\n)*?    set\(DEFAULT_EMOJI_COLLECTION )[^)]+\)")
    text, n = block.subn(rf"\g<1>{emoji_dir.resolve()})", text)
    if n == 0 and str(emoji_dir.resolve()) not in text:
        sys.exit("patch_firmware: could not find the board's emoji setting in main/CMakeLists.txt")
    cmake.write_text(text, encoding="utf-8")
    patch(root / "scripts/build_default_assets.py",
          "    if not default_emoji_collection:\n        return None\n",
          "    if not default_emoji_collection:\n        return None\n"
          "    if os.path.isdir(default_emoji_collection):  # custom folder of PNG/GIF faces\n"
          "        return default_emoji_collection\n")
    print(f"patched: face animations from {emoji_dir} ({len(list(emoji_dir.glob('*.gif')))} GIFs)")

# --- 4. double-tap, wake channel and motion gestures -----------------------------------
src = board_dir / f"{board}.cc"
AGENT_CODE = r"""
    // ======================================================================
    // Pi-Esp Jarvis additions
    //   * double-tap the screen to talk / interrupt
    //   * wake channel: an idle board waits on the Pi so alarms/timers can wake it
    //   * motion gestures (QMI8658): face-down = quiet/snooze, shake = stop/cancel
    // ======================================================================
    int64_t last_tap_us_ = 0;
    i2c_master_dev_handle_t imu_ = nullptr;
    volatile float last_z_ = 0;
    volatile int face_up_sign_ = 1;

    static void OnScreenTap(lv_event_t* e) {
        auto* self = static_cast<WaveshareEsp32s3TouchAMOLED1inch8*>(lv_event_get_user_data(e));
        // A tap means the screen is facing the user: learn which way up the sensor is.
        float z = self->last_z_;
        if (self->imu_ != nullptr && fabsf(z) > 0.8f) {
            int sign = z > 0 ? 1 : -1;
            if (sign != self->face_up_sign_) {
                self->face_up_sign_ = sign;
                Settings settings("agent", true);
                settings.SetInt("face_up", sign);
            }
        }
        int64_t now = esp_timer_get_time();
        if (now - self->last_tap_us_ > 450000) {  // first tap: wait for a second one
            self->last_tap_us_ = now;
            return;
        }
        self->last_tap_us_ = 0;
        Application::GetInstance().Schedule([self]() {
            self->power_save_timer_->WakeUp();
            auto& app = Application::GetInstance();
            auto state = app.GetDeviceState();
            if (state == kDeviceStateIdle || state == kDeviceStateSpeaking) {
                ESP_LOGI(TAG, "Double tap");
                app.ToggleChatState();
            }
        });
    }

    void InitializeDoubleTap() {
        if (!lvgl_port_lock(0)) {
            return;
        }
        lv_obj_t* layer = lv_layer_top();
        lv_obj_add_flag(layer, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(layer, OnScreenTap, LV_EVENT_SHORT_CLICKED, this);
        lvgl_port_unlock();
    }

    // The Pi's address, taken from the same setting the firmware uses at boot.
    static std::string ServerBase() {
        Settings settings("wifi", false);
        std::string url = settings.GetString("ota_url");
        if (url.empty()) {
            url = CONFIG_OTA_URL;
        }
        auto pos = url.find("/xiaozhi/");
        return pos == std::string::npos ? url : url.substr(0, pos) + "/xiaozhi/";
    }

    static void WakeChannelTask(void* arg) {
        auto* self = static_cast<WaveshareEsp32s3TouchAMOLED1inch8*>(arg);
        vTaskDelay(pdMS_TO_TICKS(15000));
        const std::string url = ServerBase() + "events?device=" + SystemInfo::GetMacAddress();
        ESP_LOGI(TAG, "Wake channel: %s", url.c_str());
        while (true) {
            if (Application::GetInstance().GetDeviceState() != kDeviceStateIdle) {
                vTaskDelay(pdMS_TO_TICKS(2000));
                continue;
            }
            bool wake = false;
            bool ok = false;
            {
                auto http = self->GetNetwork()->CreateHttp(0);
                http->SetTimeout(35000);
                if (http->Open("GET", url)) {
                    auto code = http->GetStatusCode();
                    if (code && *code == 200) {
                        ok = true;
                        wake = http->ReadAll().find("\"wake\"") != std::string::npos;
                    }
                    http->Close();
                }
            }
            if (wake) {
                ESP_LOGI(TAG, "Woken by the Pi");
                Application::GetInstance().Schedule([self]() {
                    self->power_save_timer_->WakeUp();
                    auto& app = Application::GetInstance();
                    if (app.GetDeviceState() == kDeviceStateIdle) {
                        app.ToggleChatState();
                    }
                });
                vTaskDelay(pdMS_TO_TICKS(3000));
            } else if (!ok) {
                vTaskDelay(pdMS_TO_TICKS(5000));  // Pi unreachable: retry gently
            }
        }
    }

    bool ImuRead(uint8_t reg, uint8_t* buf, size_t len) {
        return i2c_master_transmit_receive(imu_, &reg, 1, buf, len, 50) == ESP_OK;
    }

    bool ImuWrite(uint8_t reg, uint8_t value) {
        uint8_t b[2] = {reg, value};
        return i2c_master_transmit(imu_, b, 2, 50) == ESP_OK;
    }

    bool InitializeImu() {
        const uint8_t addresses[] = {0x6B, 0x6A};
        for (uint8_t addr : addresses) {
            if (i2c_master_probe(codec_i2c_bus_, addr, 100) != ESP_OK) {
                continue;
            }
            i2c_device_config_t cfg = {};
            cfg.dev_addr_length = I2C_ADDR_BIT_LEN_7;
            cfg.device_address = addr;
            cfg.scl_speed_hz = 400000;
            if (i2c_master_bus_add_device(codec_i2c_bus_, &cfg, &imu_) != ESP_OK) {
                imu_ = nullptr;
                continue;
            }
            uint8_t id = 0;
            if (!ImuRead(0x00, &id, 1) || id != 0x05) {  // QMI8658 WHO_AM_I
                ESP_LOGW(TAG, "Device at 0x%02x is not a QMI8658 (id 0x%02x)", addr, id);
                i2c_master_bus_rm_device(imu_);
                imu_ = nullptr;
                continue;
            }
            ImuWrite(0x02, 0x40);  // CTRL1: register auto-increment, little endian
            ImuWrite(0x03, 0x16);  // CTRL2: accelerometer +-4 g
            ImuWrite(0x08, 0x01);  // CTRL7: accelerometer on
            ESP_LOGI(TAG, "QMI8658 motion sensor at 0x%02x", addr);
            return true;
        }
        ESP_LOGW(TAG, "No motion sensor found: gestures disabled");
        return false;
    }

    void SendGesture(const char* type) {
        ESP_LOGI(TAG, "Gesture: %s", type);
        auto& app = Application::GetInstance();
        auto state = app.GetDeviceState();
        if (state == kDeviceStateListening || state == kDeviceStateSpeaking) {
            app.SendMcpMessage(std::string("{\"jsonrpc\":\"2.0\",\"method\":\"notifications/gesture\",\"params\":{\"type\":\"")
                               + type + "\"}}");
        } else if (state == kDeviceStateIdle) {
            auto* arg = new std::string(type);
            xTaskCreate([](void* p) {
                {
                    std::unique_ptr<std::string> kind(static_cast<std::string*>(p));
                    auto http = Board::GetInstance().GetNetwork()->CreateHttp(0);
                    http->SetHeader("Content-Type", "application/json");
                    http->SetContent("{\"device\":\"" + SystemInfo::GetMacAddress() + "\",\"type\":\"" + *kind + "\"}");
                    if (http->Open("POST", ServerBase() + "gesture")) {
                        http->ReadAll();
                        http->Close();
                    }
                }
                vTaskDelete(nullptr);
            }, "gesture_post", 6144, arg, 3, nullptr);
        }
    }

    static void GestureTask(void* arg) {
        auto* self = static_cast<WaveshareEsp32s3TouchAMOLED1inch8*>(arg);
        const float lsb_per_g = 8192.0f;  // +-4 g range
        int down_ms = 0;
        bool down_sent = false;
        int shake_hits = 0;
        int64_t shake_window = 0;
        int64_t last_shake = 0;
        while (true) {
            vTaskDelay(pdMS_TO_TICKS(50));
            uint8_t raw[6];
            if (!self->ImuRead(0x35, raw, sizeof(raw))) {
                continue;
            }
            float x = (int16_t)(raw[0] | (raw[1] << 8)) / lsb_per_g;
            float y = (int16_t)(raw[2] | (raw[3] << 8)) / lsb_per_g;
            float z = (int16_t)(raw[4] | (raw[5] << 8)) / lsb_per_g;
            self->last_z_ = z;
            float mag = sqrtf(x * x + y * y + z * z);
            int64_t now_ms = esp_timer_get_time() / 1000;

            // Face down: screen towards the table and still for 0.7 s.
            float facing = z * self->face_up_sign_;
            if (facing < -0.8f && fabsf(mag - 1.0f) < 0.25f) {
                down_ms += 50;
                if (down_ms >= 700 && !down_sent) {
                    down_sent = true;
                    self->SendGesture("face_down");
                }
            } else if (facing > -0.3f) {
                down_ms = 0;
                down_sent = false;  // picked up again: ready for the next time
            }

            // Shake: several strong jolts within one second.
            if (fabsf(mag - 1.0f) > 1.2f) {
                if (now_ms - shake_window > 1000) {
                    shake_window = now_ms;
                    shake_hits = 0;
                }
                if (++shake_hits >= 4 && now_ms - last_shake > 2500) {
                    last_shake = now_ms;
                    shake_hits = 0;
                    self->SendGesture("shake");
                }
            }
        }
    }

    void InitializeAgentExtras() {
        Settings settings("agent", false);
        face_up_sign_ = settings.GetInt("face_up", 1) < 0 ? -1 : 1;
        xTaskCreate(WakeChannelTask, "wake_channel", 6144, this, 2, nullptr);
        if (InitializeImu()) {
            xTaskCreate(GestureTask, "gestures", 3072, this, 2, nullptr);
        }
    }

public:
    WaveshareEsp32s3TouchAMOLED1inch8() :"""
text = src.read_text(encoding="utf-8")
if "InitializeAgentExtras" not in text:
    marker = "public:\n    WaveshareEsp32s3TouchAMOLED1inch8() :"
    if marker not in text:
        sys.exit(f"patch_firmware: constructor not found in {src}")
    text = text.replace(marker, AGENT_CODE.lstrip("\n"), 1)
    text, n = re.subn(r"\n(\s*)InitializeTouch\(\);\n", r"\n\1InitializeTouch();\n        InitializeDoubleTap();\n", text, count=1)
    if n != 1:
        sys.exit(f"patch_firmware: InitializeTouch() call not found in {src}")
    text, n = re.subn(r"\n(\s*)InitializeTools\(\);\n", r"\n\1InitializeTools();\n        InitializeAgentExtras();\n", text, count=1)
    if n != 1:
        sys.exit(f"patch_firmware: InitializeTools() call not found in {src}")
    extra_inc = "#include <esp_timer.h>\n#include <cmath>\n#include <memory>\n#include <string>\n#include \"system_info.h\""
    text = text.replace("#include <lvgl.h>", "#include <lvgl.h>\n" + extra_inc, 1)
    src.write_text(text, encoding="utf-8")
print("patched: double-tap to talk, wake channel, motion gestures")

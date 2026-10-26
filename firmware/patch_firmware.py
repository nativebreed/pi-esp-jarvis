#!/usr/bin/env python3
"""Customise the xiaozhi-esp32 source tree before building (runs inside the builder).

    patch_firmware.py <board_name> <ota_url> [emoji_dir]

* points the firmware at the Pi (OTA URL) and plays a chime after the wake word
* uses the "big face" screen layout on a black background
* packs our animated face GIFs (one per mood) into the firmware
* double-tap the screen to start talking (or to interrupt the answer)
"""
import json
import os
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

# --- 2b. tougher I2C at startup ---------------------------------------------------
# If a chip on the shared I2C bus doesn't answer, reset the bus and try again (3 attempts)
# before the firmware gives up, instead of going straight into a restart loop.
i2c_src = root / "main/boards/common/i2c_device.cc"
patch(i2c_src, '#include <esp_log.h>\n', '#include <esp_log.h>\n#include <freertos/FreeRTOS.h>\n#include <freertos/task.h>\n'
      '\n// Pi-Esp Jarvis: retry I2C transfers, resetting the bus between attempts\n'
      '#define JV_I2C_RETRY(bus, expr) ({ esp_err_t jv_err = ESP_FAIL; \\\n'
      '    for (int jv_try = 0; jv_try < 3 && jv_err != ESP_OK; jv_try++) { \\\n'
      '        if (jv_try > 0) { i2c_master_bus_reset(bus); vTaskDelay(pdMS_TO_TICKS(20)); } \\\n'
      '        jv_err = (expr); } \\\n'
      '    jv_err; })\n')
for old in ["ESP_ERROR_CHECK(i2c_master_transmit(i2c_device_, buffer, 2, 100));",
            "ESP_ERROR_CHECK(i2c_master_transmit(i2c_device_, tx, length + 1, 100));",
            "ESP_ERROR_CHECK(i2c_master_transmit_receive(i2c_device_, &reg, 1, buffer, 1, 100));",
            "ESP_ERROR_CHECK(i2c_master_transmit_receive(i2c_device_, &reg, 1, buffer, length, 100));"]:
    inner = old[len("ESP_ERROR_CHECK("):-2]
    patch(i2c_src, old, f"ESP_ERROR_CHECK(JV_I2C_RETRY(i2c_bus_, {inner}));")
print("patched: I2C retries at startup")

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

# --- 4. double-tap, wake channel, gestures, upright screen, dice + countdown -----------
src = board_dir / f"{board}.cc"
# If the picture shifts sideways when the screen flips, adjust with FLIP_OFFSET="x,y" (see README).
flip_x, flip_y = "DISPLAY_OFFSET_X", "DISPLAY_OFFSET_Y"
if os.environ.get("FLIP_OFFSET", "").strip():
    fx, fy = (int(v) for v in os.environ["FLIP_OFFSET"].split(","))
    flip_x, flip_y = str(fx), str(fy)
# Which way is up, as the motion sensor sees it. Measured on a v2 board ("flip the screen" log:
# axis=0 sign=1). Boards without a measurement learn it the first time they stand still.
UPRIGHT = {"esp32-s3-touch-amoled-1.8-v2": (0, 1)}
up_axis, up_sign = UPRIGHT.get(board, (-1, 1))
AGENT_CODE = r"""
    // ======================================================================
    // Pi-Esp Jarvis additions
    //   * double-tap the screen to talk / interrupt
    //   * wake channel: an idle board waits on the Pi so alarms/timers can wake it
    //   * motion gestures (QMI8658): face-down = quiet/snooze, shake = stop/cancel
    //   * upright: the screen turns 180 degrees when the board stands upside down
    //   * screen modes drawn by the board: dice (shake to roll) and timer countdown
    // ======================================================================
    int64_t last_tap_us_ = 0;
    i2c_master_dev_handle_t imu_ = nullptr;
    volatile float last_x_ = 0;
    volatile float last_y_ = 0;
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
        if (self->cam_mode_ != 0) {  // double-tap closes the camera view
            self->StopCamera();
            return;
        }
        if (self->dice_on_) {  // double-tap leaves dice mode
            self->EndDice();
            return;
        }
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

    // ------------------------------------------------------------ upright
    esp_lcd_panel_handle_t jv_panel_ = nullptr;
    esp_lcd_touch_handle_t jv_touch_ = nullptr;
    volatile bool flipped_ = false;
    volatile int up_axis_ = -1;  // sensor axis gravity runs along when the board is upright (0 = x, 1 = y)
    volatile int up_sign_ = 1;

    // The board is the right way up for whoever is looking at it right now.
    void LearnUpright(float x, float y) {
        float ax = fabsf(x), ay = fabsf(y);
        if (std::max(ax, ay) < 0.6f) {
            return;  // lying flat: nothing to learn
        }
        int axis = ay > ax ? 1 : 0;
        float v = axis ? y : x;
        int sign = (v > 0 ? 1 : -1) * (flipped_ ? -1 : 1);
        if (axis != up_axis_ || sign != up_sign_) {
            up_axis_ = axis;
            up_sign_ = sign;
            Settings settings("agent", true);
            settings.SetInt("up_axis", axis);
            settings.SetInt("up_sign", sign);
            ESP_LOGI(TAG, "Upright is axis %d, sign %d", axis, sign);
        }
    }

    void SetFlipped(bool flip) {
        if (jv_panel_ == nullptr || flip == flipped_) {
            return;
        }
        if (!lvgl_port_lock(0)) {
            return;
        }
        flipped_ = flip;
        esp_lcd_panel_mirror(jv_panel_, DISPLAY_MIRROR_X != flip, DISPLAY_MIRROR_Y != flip);
        lv_display_t* disp = lv_display_get_default();
        if (disp != nullptr) {
            lv_display_set_offset(disp, flip ? JV_FLIP_OFFSET_X : DISPLAY_OFFSET_X,
                                  flip ? JV_FLIP_OFFSET_Y : DISPLAY_OFFSET_Y);
        }
        if (jv_touch_ != nullptr) {
            esp_lcd_touch_set_mirror_x(jv_touch_, flip);
            esp_lcd_touch_set_mirror_y(jv_touch_, flip);
        }
        lv_obj_invalidate(lv_screen_active());
        lv_obj_invalidate(lv_layer_top());
        lvgl_port_unlock();
        ESP_LOGI(TAG, "Screen %s", flip ? "flipped" : "upright");
    }

    // ------------------------------------------------------- screen modes
    static constexpr uint32_t kCyan = 0x28E1FF;
    static constexpr uint32_t kAmber = 0xFFAA28;
    static constexpr int kTop = 44;     // leave the status bar visible
    static constexpr int kBottom = 56;  // and the caption
    static constexpr int kBoxW = DISPLAY_WIDTH;
    static constexpr int kBoxH = DISPLAY_HEIGHT - kTop - kBottom;
    static constexpr int kDigH = 88, kSegT = 10, kColonW = 16;

    lv_obj_t* mode_root_ = nullptr;
    lv_obj_t* dice_[2] = {};
    lv_obj_t* pips_[2][7] = {};
    lv_obj_t* timer_box_ = nullptr;
    lv_obj_t* arc_ = nullptr;
    lv_obj_t* segs_[4][7] = {};
    lv_obj_t* colon_[2] = {};
    lv_obj_t* egg_ = nullptr;
    lv_obj_t* clock_ = nullptr;
    volatile bool dice_on_ = false;
    bool timer_on_ = false;
    bool modes_hidden_ = false;
    bool caption_after_roll_ = false;
    int dice_count_ = 1;
    int dice_final_[2] = {1, 1};
    int roll_frames_ = 0;
    int digits_shown_ = 0;
    int shown_s_ = -1;
    int arc_value_ = -1;
    int timer_total_s_ = 1;
    uint32_t timer_color_ = 0;
    int64_t dice_until_ms_ = 0;
    int64_t timer_end_ms_ = 0;
    int64_t timer_zero_ms_ = 0;
    int64_t keep_awake_ms_ = 0;

    // camera pictures from the Pi (a photo, or a live view made of quick pictures)
    lv_obj_t* cam_img_ = nullptr;
    lv_obj_t* cam_badge_ = nullptr;
    lv_image_dsc_t cam_dsc_[2] = {};
    uint8_t* cam_buf_[2] = {};
    int cam_front_ = -1;
    volatile int cam_mode_ = 0;  // 0 off, 1 photo, 2 live
    std::atomic<uint32_t> cam_gen_{0};  // changes on every start/stop, so old downloads give up
    int64_t cam_until_ms_ = 0;

    static int64_t NowMs() { return esp_timer_get_time() / 1000; }

    static lv_obj_t* Plain(lv_obj_t* parent) {
        lv_obj_t* o = lv_obj_create(parent);
        lv_obj_remove_style_all(o);
        lv_obj_remove_flag(o, LV_OBJ_FLAG_CLICKABLE);  // taps fall through to the double-tap layer
        lv_obj_remove_flag(o, LV_OBJ_FLAG_SCROLLABLE);
        return o;
    }

    static void Neon(lv_obj_t* o, uint32_t color, int glow) {
        lv_obj_set_style_shadow_color(o, lv_color_hex(color), 0);
        lv_obj_set_style_shadow_width(o, glow, 0);
        lv_obj_set_style_shadow_opa(o, LV_OPA_50, 0);
    }

    void BuildModes() {  // LVGL lock held
        mode_root_ = Plain(lv_layer_top());
        lv_obj_set_size(mode_root_, kBoxW, kBoxH);
        lv_obj_set_pos(mode_root_, 0, kTop);
        lv_obj_set_style_bg_color(mode_root_, lv_color_black(), 0);
        lv_obj_set_style_bg_opa(mode_root_, LV_OPA_COVER, 0);
        lv_obj_add_flag(mode_root_, LV_OBJ_FLAG_HIDDEN);

        for (int i = 0; i < 2; i++) {
            lv_obj_t* d = Plain(mode_root_);
            lv_obj_set_style_border_color(d, lv_color_hex(kCyan), 0);
            lv_obj_set_style_border_opa(d, LV_OPA_COVER, 0);
            Neon(d, kCyan, 26);
            lv_obj_set_style_transform_pivot_x(d, LV_PCT(50), 0);
            lv_obj_set_style_transform_pivot_y(d, LV_PCT(50), 0);
            for (int k = 0; k < 7; k++) {
                lv_obj_t* pip = Plain(d);
                lv_obj_set_style_radius(pip, LV_RADIUS_CIRCLE, 0);
                lv_obj_set_style_bg_color(pip, lv_color_hex(kCyan), 0);
                lv_obj_set_style_bg_opa(pip, LV_OPA_COVER, 0);
                Neon(pip, kCyan, 14);
                pips_[i][k] = pip;
            }
            dice_[i] = d;
        }

        timer_box_ = Plain(mode_root_);
        lv_obj_set_size(timer_box_, kBoxW, kBoxH);
        arc_ = lv_arc_create(timer_box_);
        lv_obj_remove_flag(arc_, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_set_size(arc_, 300, 300);
        lv_obj_align(arc_, LV_ALIGN_CENTER, 0, 0);
        lv_arc_set_rotation(arc_, 270);
        lv_arc_set_bg_angles(arc_, 0, 360);
        lv_arc_set_range(arc_, 0, 1000);
        lv_obj_set_style_bg_opa(arc_, LV_OPA_TRANSP, 0);
        lv_obj_set_style_pad_all(arc_, 0, 0);
        lv_obj_set_style_arc_width(arc_, 14, LV_PART_MAIN);
        lv_obj_set_style_arc_width(arc_, 14, LV_PART_INDICATOR);
        lv_obj_set_style_arc_opa(arc_, LV_OPA_20, LV_PART_MAIN);
        lv_obj_set_style_arc_opa(arc_, LV_OPA_COVER, LV_PART_INDICATOR);
        lv_obj_set_style_arc_rounded(arc_, true, LV_PART_INDICATOR);
        lv_obj_set_style_pad_all(arc_, 4, LV_PART_KNOB);
        lv_obj_set_style_bg_opa(arc_, LV_OPA_COVER, LV_PART_KNOB);
        lv_obj_set_style_radius(arc_, LV_RADIUS_CIRCLE, LV_PART_KNOB);
        lv_obj_set_style_shadow_width(arc_, 16, LV_PART_KNOB);
        lv_obj_set_style_shadow_opa(arc_, LV_OPA_60, LV_PART_KNOB);

        for (auto& digit : segs_) {
            for (auto& seg : digit) {
                seg = Plain(timer_box_);
                lv_obj_set_style_radius(seg, kSegT / 2, 0);
                lv_obj_set_style_bg_opa(seg, LV_OPA_COVER, 0);
            }
        }
        for (auto& dot : colon_) {
            dot = Plain(timer_box_);
            lv_obj_set_size(dot, 11, 11);
            lv_obj_set_style_radius(dot, LV_RADIUS_CIRCLE, 0);
            lv_obj_set_style_bg_opa(dot, LV_OPA_COVER, 0);
        }
        egg_ = Plain(timer_box_);  // egg timers
        lv_obj_set_size(egg_, 46, 58);
        lv_obj_set_style_radius(egg_, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_border_width(egg_, 4, 0);
        lv_obj_set_style_bg_opa(egg_, LV_OPA_20, 0);
        lv_obj_align(egg_, LV_ALIGN_CENTER, 0, 84);
        clock_ = Plain(timer_box_);  // every other timer
        lv_obj_set_size(clock_, 48, 48);
        lv_obj_set_style_radius(clock_, LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_border_width(clock_, 4, 0);
        lv_obj_align(clock_, LV_ALIGN_CENTER, 0, 84);
        for (int h = 0; h < 2; h++) {
            lv_obj_t* hand = Plain(clock_);
            lv_obj_set_size(hand, h ? 12 : 4, h ? 4 : 16);
            lv_obj_set_style_radius(hand, 2, 0);
            lv_obj_set_style_bg_opa(hand, LV_OPA_COVER, 0);
            lv_obj_align(hand, LV_ALIGN_CENTER, h ? 5 : 0, h ? 0 : -6);
        }
        SetTimerColor(kCyan);

        cam_img_ = lv_image_create(mode_root_);
        lv_obj_remove_flag(cam_img_, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_flag(cam_img_, LV_OBJ_FLAG_HIDDEN);
        cam_badge_ = Plain(mode_root_);  // red "LIVE" tag in the corner
        lv_obj_set_size(cam_badge_, 64, 24);
        lv_obj_set_style_radius(cam_badge_, 12, 0);
        lv_obj_set_style_bg_color(cam_badge_, lv_color_hex(0xE53935), 0);
        lv_obj_set_style_bg_opa(cam_badge_, LV_OPA_COVER, 0);
        lv_obj_align(cam_badge_, LV_ALIGN_TOP_LEFT, 14, 8);
        lv_obj_t* live = lv_label_create(cam_badge_);
        lv_label_set_text(live, "LIVE");
        lv_obj_set_style_text_color(live, lv_color_white(), 0);
        lv_obj_center(live);
        lv_obj_add_flag(cam_badge_, LV_OBJ_FLAG_HIDDEN);
        lv_timer_create(ModeTick, 70, this);
    }

    void SetTimerColor(uint32_t color) {
        if (color == timer_color_) {
            return;
        }
        timer_color_ = color;
        lv_color_t c = lv_color_hex(color);
        lv_obj_set_style_arc_color(arc_, c, LV_PART_MAIN);
        lv_obj_set_style_arc_color(arc_, c, LV_PART_INDICATOR);
        lv_obj_set_style_bg_color(arc_, c, LV_PART_KNOB);
        lv_obj_set_style_shadow_color(arc_, c, LV_PART_KNOB);
        for (auto& digit : segs_) {
            for (auto* seg : digit) {
                lv_obj_set_style_bg_color(seg, c, 0);
            }
        }
        for (auto* dot : colon_) {
            lv_obj_set_style_bg_color(dot, c, 0);
        }
        for (lv_obj_t* icon : {egg_, clock_}) {
            lv_obj_set_style_border_color(icon, c, 0);
            lv_obj_set_style_bg_color(icon, c, 0);
            for (uint32_t i = 0; i < lv_obj_get_child_count(icon); i++) {
                lv_obj_set_style_bg_color(lv_obj_get_child(icon, i), c, 0);
            }
        }
    }

    void LayoutDigits(int n) {  // n digits with the colon before the last two
        if (n == digits_shown_) {
            return;
        }
        digits_shown_ = n;
        const int h = n >= 4 ? 70 : kDigH;  // four digits get smaller so they stay inside the ring
        const int w = h * 52 / 100, t = std::max(8, h * 114 / 1000), in = h * 9 / 100;
        const int half = h / 2 - h * 136 / 1000, gap = h * 11 / 100;
        const int total = n * w + kColonW + n * gap;
        int x = (kBoxW - total) / 2;
        const int top = kBoxH / 2 - 10 - h / 2;
        for (int d = 0; d < 4; d++) {
            bool used = d < n;
            for (auto* seg : segs_[d]) {
                if (used) {
                    lv_obj_remove_flag(seg, LV_OBJ_FLAG_HIDDEN);
                } else {
                    lv_obj_add_flag(seg, LV_OBJ_FLAG_HIDDEN);
                }
                lv_obj_set_style_radius(seg, t / 2, 0);
            }
            if (!used) {
                continue;
            }
            if (d == n - 2) {  // the colon sits before the last two digits
                for (int k = 0; k < 2; k++) {
                    lv_obj_set_pos(colon_[k], x + (kColonW - 11) / 2, top + h / 2 + (k ? h / 6 : -h / 6 - 11));
                }
                x += kColonW + gap;
            }
            // segments a b c d e f g
            const int geo[7][4] = {{in, 0, w - 2 * in, t},           {w - t, in, t, half},
                                   {w - t, h / 2 + in / 2, t, half}, {in, h - t, w - 2 * in, t},
                                   {0, h / 2 + in / 2, t, half},     {0, in, t, half},
                                   {in, h / 2 - t / 2, w - 2 * in, t}};
            for (int k = 0; k < 7; k++) {
                lv_obj_set_pos(segs_[d][k], x + geo[k][0], top + geo[k][1]);
                lv_obj_set_size(segs_[d][k], geo[k][2], geo[k][3]);
            }
            x += w + gap;
        }
    }

    void ShowTime(int left) {
        char buf[32];
        left = std::clamp(left, 0, 99 * 3600);
        if (left >= 3600) {
            snprintf(buf, sizeof(buf), "%d:%02d", left / 3600, (left / 60) % 60);
        } else {
            snprintf(buf, sizeof(buf), "%d:%02d", left / 60, left % 60);
        }
        static const uint8_t kSeg[10] = {0x3F, 0x06, 0x5B, 0x4F, 0x66, 0x6D, 0x7D, 0x07, 0x7F, 0x6F};
        int n = 0;
        int digits[4] = {};
        for (const char* p = buf; *p && n < 4; p++) {
            if (*p >= '0' && *p <= '9') {
                digits[n++] = *p - '0';
            }
        }
        LayoutDigits(n);
        for (int d = 0; d < n; d++) {
            for (int k = 0; k < 7; k++) {
                lv_obj_set_style_bg_opa(segs_[d][k], (kSeg[digits[d]] >> k) & 1 ? LV_OPA_COVER : LV_OPA_10, 0);
            }
        }
        SetTimerColor(left <= 60 && timer_total_s_ > 60 ? kAmber : kCyan);
    }

    void LayoutDie(int i, int size, int cx, int cy) {
        lv_obj_t* d = dice_[i];
        lv_obj_set_size(d, size, size);
        lv_obj_set_pos(d, cx - size / 2, cy - size / 2);
        lv_obj_set_style_radius(d, size * 17 / 100, 0);
        lv_obj_set_style_border_width(d, std::max(5, size * 35 / 1000), 0);
        static const int8_t kSlot[7][2] = {{-1, -1}, {1, -1}, {-1, 0}, {0, 0}, {1, 0}, {-1, 1}, {1, 1}};
        int pip = size * 17 / 100, off = size * 26 / 100;
        for (int k = 0; k < 7; k++) {
            lv_obj_set_size(pips_[i][k], pip, pip);
            lv_obj_align(pips_[i][k], LV_ALIGN_CENTER, kSlot[k][0] * off, kSlot[k][1] * off);
        }
    }

    void ShowFace(int i, int value) {
        static const uint8_t kFace[7] = {0, 0x08, 0x41, 0x49, 0x63, 0x6B, 0x77};
        for (int k = 0; k < 7; k++) {
            if ((kFace[value] >> k) & 1) {
                lv_obj_remove_flag(pips_[i][k], LV_OBJ_FLAG_HIDDEN);
            } else {
                lv_obj_add_flag(pips_[i][k], LV_OBJ_FLAG_HIDDEN);
            }
        }
    }

    void StartDice(int count, int a, int b, bool caption) {  // LVGL lock held
        dice_count_ = count;
        dice_final_[0] = a;
        dice_final_[1] = b;
        if (count == 1) {
            LayoutDie(0, 210, kBoxW / 2, kBoxH / 2);
            lv_obj_add_flag(dice_[1], LV_OBJ_FLAG_HIDDEN);
        } else {
            LayoutDie(0, 150, kBoxW / 2 - 88, kBoxH / 2 - 10);
            LayoutDie(1, 150, kBoxW / 2 + 88, kBoxH / 2 + 10);
            lv_obj_remove_flag(dice_[1], LV_OBJ_FLAG_HIDDEN);
        }
        roll_frames_ = 12;
        caption_after_roll_ = caption;
        dice_on_ = true;
        dice_until_ms_ = NowMs() + 30000;
        modes_hidden_ = false;
        UpdateModes();
    }

    void RollLocal() {  // shake while the dice are showing
        if (!lvgl_port_lock(0)) {
            return;
        }
        StartDice(dice_count_, 1 + esp_random() % 6, 1 + esp_random() % 6, true);
        lvgl_port_unlock();
    }

    void EndDice() {  // LVGL lock held
        dice_on_ = false;
        UpdateModes();
    }

    void StartTimer(int seconds, int total, const std::string& label, bool egg) {  // LVGL lock held
        timer_total_s_ = std::max(1, std::max(total, seconds));
        timer_end_ms_ = NowMs() + seconds * 1000LL;
        timer_zero_ms_ = 0;
        shown_s_ = -1;
        arc_value_ = -1;
        if (egg) {
            lv_obj_remove_flag(egg_, LV_OBJ_FLAG_HIDDEN);
            lv_obj_add_flag(clock_, LV_OBJ_FLAG_HIDDEN);
        } else {
            lv_obj_add_flag(egg_, LV_OBJ_FLAG_HIDDEN);
            lv_obj_remove_flag(clock_, LV_OBJ_FLAG_HIDDEN);
        }
        timer_on_ = true;
        modes_hidden_ = false;
        if (!label.empty()) {
            display_->SetChatMessage("system", label.c_str());
        }
        UpdateModes();
    }

    void UpdateModes() {  // LVGL lock held
        bool cam = cam_mode_ != 0;
        bool show = !modes_hidden_ && (cam || dice_on_ || timer_on_);
        if (!show) {
            lv_obj_add_flag(mode_root_, LV_OBJ_FLAG_HIDDEN);
            return;
        }
        if (cam) {  // the camera view sits on top of dice and countdowns
            for (auto* d : dice_) {
                lv_obj_add_flag(d, LV_OBJ_FLAG_HIDDEN);
            }
            lv_obj_add_flag(timer_box_, LV_OBJ_FLAG_HIDDEN);
            lv_obj_remove_flag(cam_img_, LV_OBJ_FLAG_HIDDEN);
            if (cam_mode_ == 2) {
                lv_obj_remove_flag(cam_badge_, LV_OBJ_FLAG_HIDDEN);
            } else {
                lv_obj_add_flag(cam_badge_, LV_OBJ_FLAG_HIDDEN);
            }
            lv_obj_remove_flag(mode_root_, LV_OBJ_FLAG_HIDDEN);
            lv_obj_move_foreground(mode_root_);
            return;
        }
        lv_obj_add_flag(cam_img_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(cam_badge_, LV_OBJ_FLAG_HIDDEN);
        for (auto* d : dice_) {
            if (!dice_on_) {
                lv_obj_add_flag(d, LV_OBJ_FLAG_HIDDEN);
            }
        }
        if (dice_on_) {
            lv_obj_remove_flag(dice_[0], LV_OBJ_FLAG_HIDDEN);
            lv_obj_add_flag(timer_box_, LV_OBJ_FLAG_HIDDEN);
        } else {
            lv_obj_remove_flag(timer_box_, LV_OBJ_FLAG_HIDDEN);
        }
        lv_obj_remove_flag(mode_root_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_move_foreground(mode_root_);
    }

    struct CamJob {
        WaveshareEsp32s3TouchAMOLED1inch8* self;
        uint32_t gen;
        std::string url;
        bool live;
    };

    void StartCamera(int mode, const std::string& path) {  // LVGL lock held
        uint32_t gen = ++cam_gen_;
        cam_mode_ = mode;
        cam_until_ms_ = NowMs() + (mode == 2 ? 300000 : 20000);  // live: 5 minutes, photo: 20 seconds
        modes_hidden_ = false;
        UpdateModes();
        auto* job = new CamJob{this, gen, ServerBase() + path, mode == 2};
        if (xTaskCreate(CameraTask, "camview", 8192, job, 3, nullptr) != pdPASS) {
            delete job;
            cam_mode_ = 0;
            UpdateModes();
        }
    }

    void StopCamera() {  // LVGL lock held
        ++cam_gen_;
        cam_mode_ = 0;
        lv_image_set_src(cam_img_, nullptr);
        for (int i = 0; i < 2; i++) {
            if (cam_buf_[i] != nullptr) {
                lv_image_cache_drop(&cam_dsc_[i]);
                heap_caps_free(cam_buf_[i]);
                cam_buf_[i] = nullptr;
            }
        }
        cam_front_ = -1;
        UpdateModes();
    }

    // Decode one JPEG from the Pi and put it on the screen (double-buffered).
    void ShowJpeg(const std::string& jpeg, uint32_t gen) {
        uint8_t* out = nullptr;
        size_t len = 0, w = 0, h = 0, stride = 0;
        if (jpeg_to_image(reinterpret_cast<const uint8_t*>(jpeg.data()), jpeg.size(), &out, &len, &w, &h,
                          &stride) != ESP_OK) {
            return;
        }
        if (w == 0 || h == 0 || w > (size_t)kBoxW || h > (size_t)kBoxH || !lvgl_port_lock(0)) {
            heap_caps_free(out);
            return;
        }
        if (gen != cam_gen_) {  // closed while we were downloading
            lvgl_port_unlock();
            heap_caps_free(out);
            return;
        }
        int back = cam_front_ == 0 ? 1 : 0;
        if (cam_buf_[back] != nullptr) {
            lv_image_cache_drop(&cam_dsc_[back]);
            heap_caps_free(cam_buf_[back]);
        }
        cam_buf_[back] = out;
        lv_image_dsc_t& d = cam_dsc_[back];
        memset(&d, 0, sizeof(d));
        d.header.magic = LV_IMAGE_HEADER_MAGIC;
        d.header.cf = LV_COLOR_FORMAT_RGB565;
        d.header.w = w;
        d.header.h = h;
        d.header.stride = stride;
        d.data_size = len;
        d.data = out;
        lv_image_set_src(cam_img_, &d);
        lv_obj_center(cam_img_);
        cam_front_ = back;
        lvgl_port_unlock();
    }

    static void CameraTask(void* arg) {
        std::unique_ptr<CamJob> job(static_cast<CamJob*>(arg));
        auto* self = job->self;
        int failures = 0;
        while (job->gen == self->cam_gen_) {
            int64_t t0 = NowMs();
            std::string body;
            {
                auto http = self->GetNetwork()->CreateHttp(0);
                http->SetTimeout(5000);
                if (http->Open("GET", job->url)) {
                    auto code = http->GetStatusCode();
                    if (code && *code == 200) {
                        body = http->ReadAll();
                    }
                    http->Close();
                }
            }
            if (job->gen != self->cam_gen_) {
                break;
            }
            if (!body.empty()) {
                failures = 0;
                self->ShowJpeg(body, job->gen);
            } else if (++failures >= 5) {
                ESP_LOGW(TAG, "Camera view: the Pi stopped sending pictures");
                if (lvgl_port_lock(0)) {
                    if (job->gen == self->cam_gen_) {
                        self->StopCamera();
                    }
                    lvgl_port_unlock();
                }
                break;
            }
            if (!job->live) {
                break;  // a photo: one picture is enough
            }
            int64_t spent = NowMs() - t0;
            vTaskDelay(pdMS_TO_TICKS(spent < 90 ? 90 - spent : 5));  // at most about 10 pictures a second
        }
        vTaskDelete(nullptr);
    }

    static void ModeTick(lv_timer_t* t) {
        static_cast<WaveshareEsp32s3TouchAMOLED1inch8*>(lv_timer_get_user_data(t))->Tick();
    }

    void Tick() {  // runs in the LVGL task every 70 ms
        int64_t now = NowMs();
        if (dice_on_) {
            if (roll_frames_ > 0) {
                roll_frames_--;
                for (int i = 0; i < dice_count_; i++) {
                    int angle;
                    if (roll_frames_ == 0) {
                        ShowFace(i, dice_final_[i]);
                        angle = dice_count_ == 1 ? 0 : (i ? 60 : -80);
                    } else {
                        ShowFace(i, 1 + esp_random() % 6);
                        angle = (int)(esp_random() % 700) - 350;
                    }
                    lv_obj_set_style_transform_rotation(dice_[i], angle, 0);
                }
                if (roll_frames_ == 0 && caption_after_roll_) {
                    char text[96];
                    if (dice_count_ == 1) {
                        snprintf(text, sizeof(text), "You rolled a %d", dice_final_[0]);
                    } else {
                        snprintf(text, sizeof(text), "%d and %d - that's %d", dice_final_[0], dice_final_[1],
                                 dice_final_[0] + dice_final_[1]);
                    }
                    display_->SetChatMessage("system", text);
                }
            } else if (now > dice_until_ms_) {
                EndDice();
            }
        }
        if (timer_on_) {
            int64_t left_ms = std::max<int64_t>(0, timer_end_ms_ - now);
            int left = (int)((left_ms + 999) / 1000);
            if (left != shown_s_) {
                shown_s_ = left;
                ShowTime(left);
            }
            int value = (int)(left_ms * 1000 / (timer_total_s_ * 1000LL));
            if (value != arc_value_) {
                arc_value_ = value;
                lv_arc_set_value(arc_, value);
            }
            if (left_ms == 0) {
                if (timer_zero_ms_ == 0) {
                    timer_zero_ms_ = now;
                } else if (now - timer_zero_ms_ > 120000) {  // nobody stopped it: back to Visor
                    timer_on_ = false;
                    UpdateModes();
                }
            }
        }
        if (cam_mode_ != 0 && now > cam_until_ms_) {
            StopCamera();  // photo shown long enough / live view ran for 5 minutes
        }
        if ((cam_mode_ != 0 || dice_on_ || timer_on_) && !modes_hidden_ && now - keep_awake_ms_ > 5000) {
            keep_awake_ms_ = now;
            power_save_timer_->WakeUp();  // keep the screen on while there's something to see
        }
    }

    // "The screen is upside down": turn it round now and remember that for this way of standing.
    std::string FixFlip() {
        bool want = !flipped_;
        float x = last_x_, y = last_y_;
        if (std::max(fabsf(x), fabsf(y)) >= 0.6f) {
            int axis = fabsf(y) > fabsf(x) ? 1 : 0;
            float v = axis ? y : x;
            up_axis_ = axis;
            up_sign_ = (v > 0 ? 1 : -1) * (want ? -1 : 1);
        } else if (up_axis_ >= 0) {
            up_sign_ = -up_sign_;  // lying flat: just swap what counts as up
        }
        if (up_axis_ >= 0) {
            Settings settings("agent", true);
            settings.SetInt("up_axis", up_axis_);
            settings.SetInt("up_sign", up_sign_);
        }
        SetFlipped(want);
        char status[64];
        snprintf(status, sizeof(status), "axis=%d sign=%d flipped=%d", (int)up_axis_, (int)up_sign_, want ? 1 : 0);
        ESP_LOGI(TAG, "Flip fixed: %s", status);
        return status;
    }

    ReturnValue ScreenCommand(const std::string& mode, int a, int b, int count, int seconds, int total,
                              const std::string& label, const std::string& icon, const std::string& path) {
        if (mode == "flip_fix") {
            return FixFlip();
        }
        if (mode == "orientation") {
            char status[64];
            snprintf(status, sizeof(status), "axis=%d sign=%d flipped=%d", (int)up_axis_, (int)up_sign_,
                     flipped_ ? 1 : 0);
            return std::string(status);
        }
        if (mode_root_ == nullptr || !lvgl_port_lock(1000)) {
            return false;
        }
        if (mode == "dice") {
            StartDice(std::clamp(count, 1, 2), std::clamp(a, 1, 6), std::clamp(b, 1, 6), false);
        } else if (mode == "dice_off") {
            dice_on_ = false;
        } else if (mode == "timer") {
            StartTimer(std::max(0, seconds), total, label, icon == "egg");
        } else if (mode == "timer_off") {
            timer_on_ = false;
        } else if (mode == "hide") {
            modes_hidden_ = true;
        } else if (mode == "show") {
            modes_hidden_ = false;
        } else if (mode == "photo" || mode == "live") {
            if (path.empty() || path.size() > 200) {
                lvgl_port_unlock();
                return false;
            }
            StartCamera(mode == "live" ? 2 : 1, path);
        } else if (mode == "camera_off") {
            if (cam_mode_ != 0) {
                StopCamera();
            }
        } else if (mode == "off") {
            if (cam_mode_ != 0) {
                StopCamera();
            }
            dice_on_ = false;
            timer_on_ = false;
            modes_hidden_ = false;
        }
        UpdateModes();
        lvgl_port_unlock();
        return true;
    }

    void InitializeScreenModes() {
        if (lvgl_port_lock(0)) {
            BuildModes();
            lvgl_port_unlock();
        }
        McpServer::GetInstance().AddTool(
            "self.jarvis.screen",
            "Pi-Esp Jarvis screen modes, used by the Pi server: mode = dice | dice_off | timer | timer_off | "
            "hide | show | off | flip_fix | orientation | photo | live | camera_off.",
            PropertyList({Property("mode", kPropertyTypeString),
                          Property("a", kPropertyTypeInteger, 1),
                          Property("b", kPropertyTypeInteger, 1),
                          Property("count", kPropertyTypeInteger, 1),
                          Property("seconds", kPropertyTypeInteger, 0),
                          Property("total", kPropertyTypeInteger, 0),
                          Property("label", kPropertyTypeString, std::string("")),
                          Property("icon", kPropertyTypeString, std::string("timer")),
                          Property("path", kPropertyTypeString, std::string(""))}),
            [this](const PropertyList& p) -> ReturnValue {
                return ScreenCommand(p["mode"].value<std::string>(), p["a"].value<int>(), p["b"].value<int>(),
                                     p["count"].value<int>(), p["seconds"].value<int>(), p["total"].value<int>(),
                                     p["label"].value<std::string>(), p["icon"].value<std::string>(),
                                     p["path"].value<std::string>());
            });
    }

    // ------------------------------------------------- Wi-Fi from the Pi
    // The Pi's Wi-Fi page sends networks here; they join the board's saved list (newest first).
    void InitializeWifiTool() {
        McpServer::GetInstance().AddTool(
            "self.jarvis.add_wifi",
            "Pi-Esp Jarvis: save another Wi-Fi network (used by the Pi's Wi-Fi page).",
            PropertyList({Property("ssid", kPropertyTypeString),
                          Property("password", kPropertyTypeString, std::string(""))}),
            [](const PropertyList& p) -> ReturnValue {
                std::string ssid = p["ssid"].value<std::string>();
                std::string password = p["password"].value<std::string>();
                if (ssid.empty() || ssid.size() > 32 || password.size() > 64) {
                    return false;
                }
                auto& ssids = SsidManager::GetInstance();
                ssids.AddSsid(ssid, password);
                ESP_LOGI(TAG, "Saved Wi-Fi network from the Pi: %s", ssid.c_str());
                std::string names;
                for (const auto& item : ssids.GetSsidList()) {
                    if (!names.empty()) {
                        names += "\n";
                    }
                    names += item.ssid;
                }
                return names;
            });
    }

    static void GestureTask(void* arg) {
        auto* self = static_cast<WaveshareEsp32s3TouchAMOLED1inch8*>(arg);
        const float lsb_per_g = 8192.0f;  // +-4 g range
        int down_ms = 0;
        bool down_sent = false;
        int shake_hits = 0;
        int64_t shake_window = 0;
        int64_t last_shake = 0;
        int stand_ms = 0;
        int flip_ms = 0;
        int unflip_ms = 0;
        while (true) {
            vTaskDelay(pdMS_TO_TICKS(50));
            uint8_t raw[6];
            if (!self->ImuRead(0x35, raw, sizeof(raw))) {
                continue;
            }
            float x = (int16_t)(raw[0] | (raw[1] << 8)) / lsb_per_g;
            float y = (int16_t)(raw[2] | (raw[3] << 8)) / lsb_per_g;
            float z = (int16_t)(raw[4] | (raw[5] << 8)) / lsb_per_g;
            self->last_x_ = x;
            self->last_y_ = y;
            self->last_z_ = z;
            float mag = sqrtf(x * x + y * y + z * z);
            bool steady = fabsf(mag - 1.0f) < 0.2f;
            int64_t now_ms = esp_timer_get_time() / 1000;

            // Upright: turn the screen 180 degrees while the board stands upside down.
            if (self->up_axis_ < 0) {
                // Not learned yet: the first time it stands still, call that upright.
                if (steady && std::max(fabsf(x), fabsf(y)) > 0.8f) {
                    stand_ms += 50;
                    if (stand_ms >= 2000) {
                        self->LearnUpright(x, y);
                    }
                } else {
                    stand_ms = 0;
                }
            } else {
                float g = (self->up_axis_ ? y : x) * self->up_sign_;
                if (steady && g < -0.6f) {
                    unflip_ms = 0;
                    flip_ms += 50;
                    if (flip_ms >= 750 && !self->flipped_) {
                        self->SetFlipped(true);
                    }
                } else if (steady && g > 0.6f) {
                    flip_ms = 0;
                    unflip_ms += 50;
                    if (unflip_ms >= 750 && self->flipped_) {
                        self->SetFlipped(false);
                    }
                } else {
                    flip_ms = unflip_ms = 0;  // lying flat or moving: keep it as it is
                }
            }

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

            // Shake: several strong jolts within one second (rolls the dice in dice mode).
            if (fabsf(mag - 1.0f) > 1.2f) {
                if (now_ms - shake_window > 1000) {
                    shake_window = now_ms;
                    shake_hits = 0;
                }
                if (++shake_hits >= 4 && now_ms - last_shake > (self->dice_on_ ? 1200 : 2500)) {
                    last_shake = now_ms;
                    shake_hits = 0;
                    if (self->dice_on_) {
                        self->RollLocal();
                    } else {
                        self->SendGesture("shake");
                    }
                }
            }
        }
    }

    void InitializeAgentExtras() {
        Settings settings("agent", false);
        face_up_sign_ = settings.GetInt("face_up", 1) < 0 ? -1 : 1;
        up_axis_ = settings.GetInt("up_axis", JV_UP_AXIS);  // board default; "flip the screen" overrides it
        up_sign_ = settings.GetInt("up_sign", JV_UP_SIGN) < 0 ? -1 : 1;
        InitializeScreenModes();
        InitializeWifiTool();
        xTaskCreate(WakeChannelTask, "wake_channel", 6144, this, 2, nullptr);
        if (InitializeImu()) {
            xTaskCreate(GestureTask, "gestures", 4096, this, 2, nullptr);
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
    text, n = re.subn(r"\n(\s*)esp_lcd_panel_disp_on_off\(panel, true\);\n",
                      r"\n\1esp_lcd_panel_disp_on_off(panel, true);\n\1jv_panel_ = panel;\n", text, count=1)
    if n != 1:
        sys.exit(f"patch_firmware: display setup not found in {src}")
    def _touch_retry(m: re.Match) -> str:
        i, fn = m.group(1), m.group(2)
        lines = ["esp_err_t touch_err = ESP_FAIL;",
                 "for (int attempt = 0; attempt < 3 && touch_err != ESP_OK; attempt++) {",
                 "    if (attempt > 0) {  // a chip that didn't answer: free the bus and try again",
                 "        i2c_master_bus_reset(codec_i2c_bus_);",
                 "        vTaskDelay(pdMS_TO_TICKS(200));",
                 "    }",
                 f"    touch_err = {fn}(tp_io_handle, &tp_cfg, &tp);",
                 "}",
                 "if (touch_err != ESP_OK) {",
                 '    ESP_LOGE(TAG, "Touch screen not responding: carrying on without touch");',
                 "    return;",
                 "}",
                 "jv_touch_ = tp;"]
        return "\n" + "".join(i + line + "\n" for line in lines)
    text, n = re.subn(r"\n(\s*)ESP_ERROR_CHECK\((esp_lcd_touch_new_i2c_\w+)\(tp_io_handle, &tp_cfg, &tp\)\);\n",
                      _touch_retry, text, count=1)
    if n != 1:
        sys.exit(f"patch_firmware: touch setup not found in {src}")
    extra_inc = ("#include <esp_timer.h>\n#include <esp_random.h>\n#include <algorithm>\n#include <cmath>\n"
                 "#include <cstdio>\n#include <memory>\n#include <string>\n#include \"system_info.h\"\n"
                 "#include \"mcp_server.h\"\n#include <ssid_manager.h>\n#include <cstring>\n"
                 "#include <esp_heap_caps.h>\n#include \"jpeg_to_image.h\"\n#include <src/misc/cache/lv_cache.h>\n#include <atomic>\n"
                 f"#define JV_FLIP_OFFSET_X {flip_x}\n#define JV_FLIP_OFFSET_Y {flip_y}\n"
                 f"#define JV_UP_AXIS {up_axis}\n#define JV_UP_SIGN {up_sign}")
    text = text.replace("#include <lvgl.h>", "#include <lvgl.h>\n" + extra_inc, 1)
    src.write_text(text, encoding="utf-8")
print("patched: double-tap, wake channel, gestures, upright screen, dice and timer screens")

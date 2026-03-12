import ctypes
import ctypes.wintypes
import atexit
import signal
import sys
import winreg
import threading
import time

# === Windows API ===

gdi32 = ctypes.windll.gdi32
user32 = ctypes.windll.user32

# Структуры для EnumDisplayDevices
DISPLAY_DEVICE_PRIMARY_DEVICE = 0x00000004

class DISPLAY_DEVICE(ctypes.Structure):
    _fields_ = [
        ('cb', ctypes.wintypes.DWORD),
        ('DeviceName', ctypes.wintypes.WCHAR * 32),
        ('DeviceString', ctypes.wintypes.WCHAR * 128),
        ('StateFlags', ctypes.wintypes.DWORD),
        ('DeviceID', ctypes.wintypes.WCHAR * 128),
        ('DeviceKey', ctypes.wintypes.WCHAR * 128),
    ]


def get_primary_monitor_name():
    """Получить имя основного монитора"""
    i = 0
    while True:
        device = DISPLAY_DEVICE()
        device.cb = ctypes.sizeof(device)

        if not user32.EnumDisplayDevicesW(None, i, ctypes.byref(device), 0):
            break

        # Проверяем флаг PRIMARY_DEVICE
        if device.StateFlags & DISPLAY_DEVICE_PRIMARY_DEVICE:
            return device.DeviceName

        i += 1

    # Если не нашли основной, возвращаем первый доступный
    return None


def get_dc():
    """Получить DC только для основного монитора"""
    monitor_name = get_primary_monitor_name()
    if monitor_name:
        # CreateDC для конкретного монитора
        hdc = gdi32.CreateDCW(monitor_name, None, None, None)
        if hdc:
            return hdc

    # Fallback на весь экран если что-то пошло не так
    return user32.GetDC(0)


def release_dc(hdc):
    """Освободить DC"""
    # Для CreateDC используется DeleteDC, для GetDC - ReleaseDC
    # Пробуем оба варианта (один вернёт 0, другой успешно выполнится)
    gdi32.DeleteDC(hdc)


def get_gamma_ramp(hdc):
    ramp = (ctypes.c_ushort * 256 * 3)()
    result = gdi32.GetDeviceGammaRamp(hdc, ctypes.byref(ramp))
    if not result:
        print("[WARN] GetDeviceGammaRamp failed")
    return ramp


def set_gamma_ramp(hdc, ramp):
    result = gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp))
    if not result:
        print("[WARN] SetDeviceGammaRamp failed — Windows rejected the gamma ramp")
    return result


def ensure_gamma_range():
    """
    Windows ограничивает диапазон SetDeviceGammaRamp.
    Ключ реестра GdiICMGammaRange = 256 снимает ограничение.
    Требует прав администратора.
    """
    key_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ICM"
    value_name = "GdiICMGammaRange"
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0,
                             winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
        try:
            val, _ = winreg.QueryValueEx(key, value_name)
            winreg.CloseKey(key)
            if val == 256:
                return True
        except FileNotFoundError:
            winreg.CloseKey(key)
    except FileNotFoundError:
        pass

    # Пробуем установить значение
    try:
        key = winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, key_path, 0,
                                 winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY)
        winreg.SetValueEx(key, value_name, 0, winreg.REG_DWORD, 256)
        winreg.CloseKey(key)
        print("[OK] Registry: GdiICMGammaRange set to 256 (extended range enabled)")
        print("[!]  Может потребоваться перезагрузка ПК для вступления в силу.")
        return True
    except PermissionError:
        print("[ERR] Нет прав для записи в реестр. Запустите от администратора!")
        return False


# === Построение гамма-кривой ===

def build_gamma_ramp(brightness=1.0, contrast=1.0, gamma=1.0, green_tint=0.0):
    """
    green_tint: 0.0-0.3 (0 = нет оттенка, >0 = зеленый оттенок как ПНВ)
    """
    ramp = (ctypes.c_ushort * 256 * 3)()

    for i in range(256):
        value = i / 255.0
        value = pow(value, gamma)
        value = (value - 0.5) * contrast + 0.5
        value *= brightness
        value = max(0.0, min(1.0, value))

        # Применяем зеленый оттенок (усиливаем зеленый канал)
        red_value = value * (1.0 - green_tint * 0.5)
        green_value = value * (1.0 + green_tint)
        blue_value = value * (1.0 - green_tint * 0.5)

        ramp[0][i] = int(max(0.0, min(1.0, red_value)) * 65535)    # Red
        ramp[1][i] = int(max(0.0, min(1.0, green_value)) * 65535)  # Green
        ramp[2][i] = int(max(0.0, min(1.0, blue_value)) * 65535)   # Blue

    return ramp


# === Пресеты ===

PRESETS = {
    "off":    {"brightness": 1.0, "contrast": 1.0, "gamma": 1.0, "green_tint": 0.0},
    "light":  {"brightness": 1.1, "contrast": 1.1, "gamma": 0.7, "green_tint": 0.15},
    "medium": {"brightness": 1.2, "contrast": 1.2, "gamma": 0.5, "green_tint": 0.2},
    "strong": {"brightness": 1.3, "contrast": 1.3, "gamma": 0.35, "green_tint": 0.25},
}

preset_names = list(PRESETS.keys())
current_index = 0

# === Сохранить оригинальную гамму ===

hdc = get_dc()
original_ramp = get_gamma_ramp(hdc)
release_dc(hdc)


def restore_original():
    hdc = get_dc()
    set_gamma_ramp(hdc, original_ramp)
    release_dc(hdc)
    print("Gamma restored")


atexit.register(restore_original)
signal.signal(signal.SIGINT, lambda *_: exit(0))


# === Применение фильтра ===

def apply_filter(brightness, contrast, gamma, green_tint=0.0):
    hdc = get_dc()
    ramp = build_gamma_ramp(brightness, contrast, gamma, green_tint)
    ok = set_gamma_ramp(hdc, ramp)
    release_dc(hdc)
    return ok


def cycle_preset():
    global current_index
    current_index = (current_index + 1) % len(preset_names)
    name = preset_names[current_index]
    preset = PRESETS[name]
    ok = apply_filter(**preset)
    status = "OK" if ok else "FAIL"
    tint_str = f", tint={preset['green_tint']}" if preset.get('green_tint', 0) > 0 else ""
    print(f"Preset: {name} [{status}]  (b={preset['brightness']}, c={preset['contrast']}, g={preset['gamma']}{tint_str})")


def reset_filter():
    global current_index
    current_index = 0
    apply_filter(1.0, 1.0, 1.0)
    print("Filter OFF")


# === Горячие клавиши через GetAsyncKeyState ===
# Virtual Key Codes — не зависят от раскладки (русская/английская)
# VK_B (0x42) = физическая клавиша B/И
# VK_N (0x4E) = физическая клавиша N/Т

VK_B = 0x42     # Клавиша B (или И на русской)
VK_N = 0x4E     # Клавиша N (или Т на русской)
VK_F12 = 0x7B   # F12

b_pressed = False
n_pressed = False
f12_pressed = False
stop_listener = threading.Event()

def key_listener_thread():
    """Поток для отслеживания клавиш через GetAsyncKeyState (работает даже при зажатых WASD)"""
    global b_pressed, n_pressed, f12_pressed

    while not stop_listener.is_set():
        # Проверяем B
        if user32.GetAsyncKeyState(VK_B) & 0x8000:
            if not b_pressed:
                b_pressed = True
                cycle_preset()
        else:
            b_pressed = False

        # Проверяем N
        if user32.GetAsyncKeyState(VK_N) & 0x8000:
            if not n_pressed:
                n_pressed = True
                reset_filter()
        else:
            n_pressed = False

        # Проверяем F12
        if user32.GetAsyncKeyState(VK_F12) & 0x8000:
            if not f12_pressed:
                f12_pressed = True
                stop_listener.set()
        else:
            f12_pressed = False

        time.sleep(0.05)  # 50ms между проверками


# === Main ===

def main():
    print("=== ARC Raiders Screen Filter ===")
    print()

    # Проверяем/устанавливаем расширенный диапазон гаммы
    ensure_gamma_range()
    print()

    # Диагностика: пробуем применить тестовую гамму
    print("[TEST] Applying test gamma ramp...")
    test_ok = apply_filter(1.1, 1.0, 0.8)
    if test_ok:
        print("[TEST] SetDeviceGammaRamp works! Resetting...")
        apply_filter(1.0, 1.0, 1.0)
    else:
        print("[TEST] SetDeviceGammaRamp FAILED.")
        print("       Попробуйте:")
        print("       1. Запустить от администратора")
        print("       2. Перезагрузить ПК (если реестр был изменён)")
        print("       3. Проверить настройки HDR в Windows (выключить HDR)")
        print()

    print("B (И) — переключить пресет (работает на ходу, любая раскладка!)")
    print("N (Т) — выключить фильтр")
    print("F12   — выход")
    print(f"Текущий пресет: {preset_names[current_index]}")
    print()

    # Запуск потока отслеживания клавиш
    listener = threading.Thread(target=key_listener_thread, daemon=True)
    listener.start()

    # Ждём сигнала выхода
    stop_listener.wait()
    print("\nExiting...")


if __name__ == "__main__":
    main()

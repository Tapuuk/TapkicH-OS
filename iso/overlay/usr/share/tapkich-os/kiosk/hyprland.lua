-- TapkicH-OS live ISO: the installer kiosk. One floating glass terminal running the installer over the dusk picture.
-- No keybinds, no bar, nothing else. Started by /usr/local/bin/tapkich-kiosk on the first console.
-- Look = the approved mockup (design/glass, "smoke", calm): 2 px light rim, 20 px corners. The background is the
-- boot menu's pre-blurred dusk picture (the same scene from boot menu to installer; compositor blur is not reliable
-- on software rendering, which is what VMs without 3D get).

hl.monitor({ output = "", mode = "preferred", position = "auto", scale = 1 })

hl.config({
    general = {
        gaps_in = 0,
        gaps_out = 0,
        border_size = 2,
        col = {
            -- the glass rim: brighter at the top, faint at the bottom (white only, no colour)
            active_border = { colors = { "rgba(ffffff4d)", "rgba(ffffff12)" }, angle = 90 },
            inactive_border = "rgba(ffffff12)",
        },
        resize_on_border = false,
    },
    decoration = {
        rounding = 20,
        rounding_power = 2,
        shadow = { enabled = true, range = 60, render_power = 3, color = 0x6b000000 },
        blur = { enabled = true, size = 10, passes = 3, ignore_opacity = true, new_optimizations = true },
    },
    animations = { enabled = true },
    input = { kb_layout = "us", follow_mouse = 1 },
    cursor = { inactive_timeout = 3 },
    misc = {
        disable_hyprland_logo = true,
        disable_splash_rendering = true,
        force_default_wallpaper = 0,
        background_color = 0x0b0a10,
    },
    ecosystem = { no_update_news = true, no_donation_nag = true },
})

-- A soft spring as the window appears (snappy, a little bounce), quick fades
hl.curve("easy", { type = "spring", mass = 1, stiffness = 238.1191, dampening = 24.21279333 })
hl.curve("quick", { type = "bezier", points = { { 0.15, 0 }, { 0.1, 1 } } })
hl.animation({ leaf = "windowsIn", enabled = true, speed = 4.1, spring = "easy", style = "popin 90%" })
hl.animation({ leaf = "windowsOut", enabled = true, speed = 2, bezier = "quick", style = "popin 90%" })
hl.animation({ leaf = "fade", enabled = true, speed = 3, bezier = "quick" })
hl.animation({ leaf = "layersIn", enabled = true, speed = 4, bezier = "quick", style = "fade" })

hl.window_rule({
    name = "tapkich-installer",
    match = { class = "^tapkich-installer$" },
    float = true,
    size = "monitor_w*0.72 monitor_h*0.78",
    center = true,
})

-- Every screen shows the same (the installer and the picture): mirror.sh sets all other screens to mirror the first one.
-- Run at start and whenever a monitor is plugged in.
hl.on("monitor.added", function()
    hl.exec_cmd("bash /usr/share/tapkich-os/kiosk/mirror.sh")
end)

hl.on("hyprland.start", function()
    hl.exec_cmd("bash /usr/share/tapkich-os/kiosk/mirror.sh")
    hl.exec_cmd("swaybg -m fill -i /opt/tapkich-os/tapkich_os/data/limine/wallpaper.jpg")
    hl.exec_cmd("/usr/local/bin/tapkich-kiosk-term")
end)

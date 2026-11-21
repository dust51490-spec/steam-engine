# Local settings of Steam Engine on the Windows PC: all keys with their defaults.
# Live: %LOCALAPPDATA%\SteamEngine\settings.psd1 (created by Install-SteamEngine.ps1; -MqttHost
# changes it, with -Update too). A missing value takes the default from Get-SteamEngineConfig
# (mqtt_publish.ps1). First installs go to Steam's default library (the star in Steam > Settings >
# Storage); there is no setting for it.
@{
    # Address of the MQTT broker (Mosquitto in Home Assistant)
    MqttHost = 'homeassistant.local'
    MqttPort = 1883
    # Name of this PC: only for a second PC on the same Home Assistant (its topics, device and entity
    # IDs then carry it, see PROTOCOL.md); empty for the first PC. Set by the setup, not changed later.
    PcName   = ''
}

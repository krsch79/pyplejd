# Pyplejd

Python package for communicating with and controlling [Plejd](https://plejd.com) devices with [Home Assistant](https://home-assistant.io)

## Kristian reliability fork

Version `0.21.3.post7` is a focused reliability build for Home Assistant 2026.8
and Plejd firmware 6.43.x. It uses direct BLE authentication first, validates
the actual Bleak connection, and can route an address-specific control command
directly to the Plejd hardware that owns the output. This avoids firmware's
broken forwarding path where a mesh command can be echoed without changing the
remote physical output. The companion integration can opt into an on-demand
mode that confirms each command and then disconnects from the target, avoiding
the firmware 6.43.3 output oscillation seen with long-lived controller
connections. Persistent gateway preferences still control automatic selection
outside that mode, and an explicit control write can temporarily connect to a
blacklisted target node.

The older non-gateway recovery remains available for commands that cannot be
mapped to one hardware node. It accepts only a fresh NodeIndexData poll matching
both power state and raw dim level; LastChangedDataVector echoes are never used
as delivery proof.

The companion `krsch79/hass-plejd` fork enables direct control routing and the
non-gateway fallback, and disables active button-event polling to prevent a
feedback storm. Keep that integration pinned to the exact pyplejd commit used
for deployment.

---

Contributors not listed in git history - in no particular order:

- [@astrandb](https://github.com/astrandb)
- [@bnordli](https://github.com/bnordli)
- [@oyvindwe](https://github.com/oyvindwe)
- [@NewsGuyTor](https://github.com/NewsGuyTor)
- [@genesiscrew](https://github.com/genesiscrew)
- [@MHultman](https://github.com/MHultman)

---

**INFORMATION BELOW IS DEPRECATED**
To be updated soon

Much information below is taken from here: https://github.com/icanos/hassio-plejd/issues/163

Other things I have disocvered myself

## BLE communication

### Characteristics:

| Name                  |                                | Description                                                                 |
| --------------------- | ------------------------------ | --------------------------------------------------------------------------- |
|                       | `-6085-4726-be45-040c957391b5` | Suffix for all characteristics                                              |
| Service               | `31ba0001-`                    | Main service indicator.                                                     |
| NodeIndexData         | `31ba0003-`                    | Basic status updates.                                                       |
| DataVector            | `31ba0004-`                    | Sending commands to the mesh.                                               |
| LastChangedDataVector | `31ba0005-`                    | Bi-directional communication of state. Also works for commands to the mesh. |
| AuthKey               | `31ba0009-`                    | For cryptographic authentication with the mesh.                             |
| PingPong              | `31ba000a-`                    | Keep-alive signal.                                                          |

### Authentication process

```
- Establish BLE Connection from Controller (C) to Gateway Device (GD)
- C -> GD(AuthKey): 0x00
- C <- GD(AuthKey): <challenge>
- C -> GD(AuthKey): <response>


<challenge>: 32 bytes
<response>:
    a = <challenge> XOR <mesh key>
    b = sha256(a)
    <response> = b[1-16] XOR b[17-32]
<mesh key>: 32 bytes system unique key received from Cloud
```

Perform a ping to confirm authentication

### Ping

Ping needs to be performed at regular intervals for the Gateway Device to maintain the connection.

```
- C -> GD(PingPong): <ping>
- C <- GD(PingPong): <pong>


<ping>: Random integer
<pong>: <ping> + 1
```

### NodeIndexData

#### Poll state

```
- C -> GD(NodeIndexData) 0x1
```

#### State Report

```
- GD -> C(NodeIndexData) list[<lightlevel>]


Lights and relays:
<lightlevel>: 10 bytes
    AA SS XX XX XX XX DD DD XX XX
    AA - Device address
    SS - Device state (boolean)
    DD - Device dim level

Cover:
<lightlevel> (bits): AAAA AAAA MMMM MMMM [32X] UPPP PPPP XTTT TTTT
    A - Device Address
    Others as for Output State and Level command

Thermostat:
<lightlevel> (bits): AAAA AAAA XXXX XXXM [32X] MMET TTTT TTCC CCCC
    A - Device Address
    Others as for Output State and Level command
```

### LastChangedDataVector

#### Encryption and Decryption

```
function encrypt_decrypt(<key> <address> <data>)
    cipher = AES-128-ECB(<key>)
    a = cipher.encrypt(key .. key .. key[1-4])
    return <data> XOR mod(a, 16)
```

#### Status updates from mesh (IN)

```
- GD -> C(LastChangedDataVector): <encrypted data>
<data> = encrypt_decrypt(<mesh key> <GD address> <encrypted data>)
```

#### Commands to mesh (OUT)

```
- C -> GD(LastChangedDataVector): <encrypted data>
or
- C -> GD(DataVector): <encrypted data>

<encrypted data> = encrypt_decrypt(<mesh key> <GD address> <data>)
```

#### Data

```
<data> format:
    AA VV TT CC CC PAYLOAD

    AA - Device Address
    VV - Protocol version (always 0x01)
    TT - Command type
        0x00 - write
        0x01 - Acknowledgement
        0x02 - Reply
        0x10 - Do Not Respond
        Usually Do Not Respond for commands to the mesh
    CC CC - Commmand
```

### Commands

#### **`0x0015` - Event Prepare (OUT)**

Sending this command will cause ALL buttons to send the Button Pressed command when pressed.
Otherwise only battery powered buttons (WPH-01) will do so.

This is what the Plejd app uses to identify a button when programming it. As far as I can tell, this does not affect the normal operation of the buttons.

Broadcast command, so the address should be 0x00.

```
PAYLOAD: None
```

#### **`0x0016` - Event Fired (IN)**

Sent by buttons when pressed after receiving Event Prepare command. Only sent once per Event Prepare command.

```
PAYLOAD: AA BB [RR]
    AA - Address
    BB - Button index
    RR - 0 when button is released after a long press
```

#### **`0x0021` - Scene (IN/OUT)**

This is a broadcast event, so the Device Address is always 0.

```
PAYLOAD: SS
    SS - Scene ID
```

#### **`0x0097` - Output State**

```
Lights and Relays (IN/OUT)
PAYLOAD: SS
    SS - state on/off
```

#### **`0x0098`/`0x00C8` - Output State and Level**

```
Lights and Relays (IN/OUT)
PAYLOAD: SS [DD DD]
    SS - state on/off
    DD - Two bytes dim level (optional)
Tip: Send the same byte DD twice. That gives 255 dim levels and you don't have to worry about endianness. This is what the Plejd app seems to do.

Cover
PAYLOAD (bits): MMMM MMMM UPPP PPPP XTTT TTTT
    M - Moving (boolean)
    U - 1: moving up, 0: moving down
    P - Position, 7 bit integer
    T - Target position, 7 bit integer

Thermostat
PAYLOAD (bits): XXXX XXXM MMET TTTT TTCC CCCC
    M - Mode
    E - Error
    T - Target temperature
    C - Current temperature

    Modes:
    0 - service/off
    2 - vacation
    3 - boots
    4 - frost protection
    5 - night
    6 - low
    7 - normal
```

#### **`0x0420` - Output Set**

```
PAYLOAD: list[<minipackage>]

<minipackage> (bits): 0LLL TTTT <data>
                  or: 0LLL 1111 TTTT <data>
    L - 1 less than the number of bytes in <data>
    T - Type. One byte or 0xF + one byte

    Types
    0001 - Whitebalance
    0011 - Source
    0110 - Lux
    0111 - Window Control
    1111 0001 - Channel
    1111 0111 - Battery Info
    1111 1001 - Tilt
```

Each command may include several minipackages

- Color temperature of light (IN):

```
minipackage(type=Whitebalance, data=<wb>)

<wb>: Color temperature in K
```

- Motion detected (IN):

```
minipackage(type=Source, data=0x03)
```

- Battery level (IN):

```
minipackage(type=Battery Info, data=<charge level>)

<charge level>: Probably 0-255
```

- Ambient light (IN):

```
minipackage(type=Lux, data=<ll>)

<ll>: Light Level. 0x2 is above threshold. Anything else is below.
```

- Set color temperature of light (OUT):

```
PAYLOAD: minipackage(type=Source, data=0x01) minipackage(type=Whitebalance, data=<wb>)

<wb>: Color temperature in K. 2 bytes
```

- Set position of cover (OUT):

```
PAYLOAD: minipackage(type=Source, data=0x08) [minipackage(type=Window Control, data=0x1 <level> <level>)] [minipackage(type=Tilt, data=<tilt>)]

<level>: Position 0-255
<tilt>: Tilt position 0-255
```

- Stop movement of cover (OUT):

```
PAYLOAD: minipackage(type=Source, data=0x08) minipackage(type=Window Control, data=0)
```

#### **`0x045C` - Thermostat Temperature Setpoint**

```
IN:
PAYLOAD: XX XX XX XX XX XX TT TT

    TT - Temperature (little-endian)

OUT:
PAYLOAD: TT TT
    TT - Temperature (little-endian) in units of 0.1 degC
```

#### **`0x0461` - Thermostat Mode (OUT)**

```
PAYLOAD: MM
    MM - Operating mode (same as Output State and Level)
```

#### **`0x0461` - Thermostat PWM Setpoint**

```
IN:
PAYLOAD: XX XX XX XX XX XX SS

    SS - Setpoint

OUT:
PAYLOAD: SS
    SS - Setpoint
```

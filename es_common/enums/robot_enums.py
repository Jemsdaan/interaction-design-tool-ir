from es_common.enums.es_enum import ESEnum


class RobotName(ESEnum):
    NAO = 0
    PEPPER = 1


class SpeechActsType(ESEnum):
    FORMAL = 0
    INFORMAL = 1


class VoiceTag(ESEnum):
    SPEED = "rspd"
    PITCH = "vct"
    PROSODY = "bound"
    STYLE = "style"
    VOLUME = "vol"
    PAUSE = "pau"
    RESET = "rst"
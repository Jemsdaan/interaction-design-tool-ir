import logging
import time

from twisted.internet.defer import inlineCallbacks

from es_common.model.interaction_block import InteractionBlock
from es_common.utils.db_helper import DBHelper
from robot_manager.pepper.handler.animation_handler import AnimationHandler
from robot_manager.pepper.handler.connection_handler import ConnectionHandler
from robot_manager.pepper.handler.speech_handler import SpeechHandler
from robot_manager.pepper.handler.tablet_handler import TabletHandler
from thread_manager.db_thread import DBChangeStreamThread

TABLET_IP = "198.18.0.1"


class RobotWorker(object):
    def __init__(self, robot_name=None, robot_realm=None):
        self.logger = logging.getLogger("RobotWorker")

        self.robot_name = robot_name
        self.robot_realm = robot_realm

        self.db_helper = DBHelper()
        self.db_change_thread = None

        self.speech_handler = None
        self.tablet_handler = None
        self.animation_handler = None
        self.connection_handler = None

        self.interaction_block = None
        self.is_interacting = False

    def connect_robot(self, data_dict=None):
        try:
            self.connection_handler = ConnectionHandler()
            # self.connection_handler.session_observers.add_observer(self.on_connect)

            self.logger.info("Connecting...")
            self.connection_handler.start_rie_session(robot_name=self.robot_name,
                                                      robot_realm=self.robot_realm,
                                                      callback=self.on_connect)

            self.logger.info("Successfully connected to the robot")
        except Exception as e:
            self.logger.error("Error while connecting to the robot: {}".format(e))

    @inlineCallbacks
    def on_connect(self, session, details=None):
        try:
            yield self.logger.debug("Received session: {}".format(session))
            self.speech_handler = SpeechHandler(session=session)
            self.speech_handler.block_completed_observers.add_observer(self.on_block_executed)
            self.speech_handler.keyword_observers.add_observer(self.on_user_answer)

            self.tablet_handler = TabletHandler(session=session)
            self.animation_handler = AnimationHandler(session=session)

            # update speech certainty
            self.on_speech_certainty(data_dict=self.db_helper.find_one(self.db_helper.interaction_collection,
                                                                       "speechCertainty"))
            # update voice settings
            self.on_voice_pitch(data_dict=self.db_helper.find_one(self.db_helper.interaction_collection, "voicePitch"))
            self.on_voice_speed(data_dict=self.db_helper.find_one(self.db_helper.interaction_collection, "voiceSpeed"))

            self.speech_handler.set_language("en")
            self.speech_handler.animated_say("I am ready")

            # Start listening to DB Stream
            self.setup_db_stream()

            self.logger.info("Connection to the robot is successfully established.")
        except Exception as e:
            yield self.logger.error("Error while setting the robot controller {}: {}".format(session, e))

    def disconnect_robot(self, data_dict=None):
        self.logger.info("TODO: Disconnect from robot.")

    def exit_gracefully(self, data_dict=None):
        try:
            if self.db_change_thread is not None:
                self.db_change_thread.stop_running()
                self.db_helper.update_one(self.db_helper.interaction_collection,
                                          data_key="isConnected",
                                          data_dict={"isConnected": False, "timestamp": time.time()})
                time.sleep(2)
                # self.db_change_thread.join(timeout=2.0)
                if self.db_change_thread is not None and self.db_change_thread.is_alive():
                    self.logger.info("DB Thread is still alive!")

        except Exception as e:
            self.logger.error("Error while stopping thread: {} | {}".format(self.db_change_thread, e))
        finally:
            self.db_change_thread = None

    def setup_db_stream(self):
        try:
            self.db_helper.update_one(self.db_helper.robot_collection,
                                      data_key="isConnected",
                                      data_dict={"isConnected": True, "timestamp": time.time()})

            self.start_listening_to_db_stream()
            self.logger.info("Finished")
        except Exception as e:
            self.logger.error("Error while setting up db stream: {}".format(e))

    def on_user_answer(self, val=None):
        try:
            self.logger.debug("User Answer: {}".format(val))

            self.on_block_executed(val=True, execution_result="" if val is None else val)
        except Exception as e:
            self.logger.error("Error while storing user answer: {}".format(e))

    @inlineCallbacks
    def on_block_executed(self, val=None, execution_result=""):
        try:
            self.db_helper.update_one(self.db_helper.robot_collection,
                                      data_key="isExecuted",
                                      data_dict={"isExecuted": {"value": True, "executionResult": execution_result},
                                                 "timestamp": time.time()})
            yield
        except Exception as e:
            self.logger.error("Error while storing block completed: {}".format(e))

    def on_engaged(self, val=None):
        try:
            if not self.is_interacting:
                self.db_helper.update_one(self.db_helper.robot_collection,
                                          data_key="isEngaged",
                                          data_dict={"isEngaged": False if val is None else val,
                                                     "timestamp": time.time()})
        except Exception as e:
            self.logger.error("Error while storing isEngaged: {}".format(e))

    @inlineCallbacks
    def on_wakeup(self, data_dict=None):
        self.logger.info("Waking up now!")
        yield self.animation_handler.wakeup()

    @inlineCallbacks
    def on_rest(self, data_dict=None):
        self.logger.info("Resting zzz")
        yield self.animation_handler.rest()

    @inlineCallbacks
    def on_animate(self, data_dict=None):
        try:
            self.logger.info("Data received: {}".format(data_dict))

            animation_name = data_dict["animateRobot"]["animation"]
            message = data_dict["animateRobot"]["message"]
            if message is None or message == "":
                yield self.animation_handler.execute_animation(animation_name=animation_name)
            else:
                yield self.speech_handler.animated_say(message=message)
        except Exception as e:
            self.logger.error("Error while extracting animate data: {} | {}".format(data_dict, e))

    def on_hide_tablet_image(self, data_dict=None):
        try:
            self.logger.info("Data received: {}".format(data_dict))
            pass
        except Exception as e:
            self.logger.error("Error while extracting tablet image: {} | {}".format(data_dict, e))

    @inlineCallbacks
    def on_interaction_block(self, data_dict=None):
        try:
            self.logger.info("Received Interaction Block data.")
            interaction_block = self.get_interaction_block(data_dict=data_dict)

            if interaction_block is None:
                return

            # set the tablet page, if any
            if self.robot_name == "pepper":
                self.set_web_view(tablet_page=interaction_block.tablet_page)

            # get the message
            message = interaction_block.message
            if message is None or message == "":
                self.on_block_executed(val=True)
            else:
                # update the block's message, if any
                if "{answer}" in message and interaction_block.execution_result:
                    message = message.format(answer=interaction_block.execution_result.lower())

                self.logger.info("Message to say: {}".format(message))
                speech_event = self.speech_handler.animated_say(message=message)

                # check if answers are needed
                if interaction_block.topic_tag.topic == "":
                    # time.sleep(1)  # to keep the API happy :)
                    speech_event.addCallback(self.on_block_executed)
                else:
                    keywords = interaction_block.topic_tag.get_combined_answers()
                    self.speech_handler.current_keywords = keywords

                    yield speech_event.addCallback(self.speech_handler.on_start_listening)
        except Exception as e:
            self.logger.error("Error while extracting interaction block: {} | {}".format(data_dict, e))

    def get_interaction_block(self, data_dict=None):
        if data_dict is None:
            return None

        try:
            block_dict = data_dict["interactionBlock"]
            interaction_block = InteractionBlock.create_interaction_block(block_dict)
            if interaction_block:
                self.logger.info("Block's execution is in progress...")

                interaction_block.id = block_dict["id"]
                interaction_block.is_hidden = True

            return interaction_block
        except Exception as e:
            self.logger.error("Error while creating the interaction block: {}".format(e))
            return None

    @inlineCallbacks
    def set_web_view(self, tablet_page):
        if tablet_page is None:
            return None

        try:
            url_params = "?{}{}{}".format(self.check_url_parameter("pageHeading", tablet_page.heading),
                                          self.check_url_parameter("pageText", tablet_page.text),
                                          self.check_url_parameter("pageImage", tablet_page.image))

            self.tablet_handler.show_offline_page(name=tablet_page.name, url_params=url_params)
        except Exception as e:
            self.logger.error("Error while constructing the tablet URL: {}".format(e))

    def check_url_parameter(self, param_name, param_value):
        if param_value is not None and param_value != "":
            return "{}={}&".format(param_name, param_value)

        return ""

    def on_start_interaction(self, data_dict=None):
        try:
            self.logger.info("Data received: {}".format(data_dict))
            self.is_interacting = data_dict["startInteraction"]
        except Exception as e:
            self.logger.error("Error while extracting interaction data: {} | {}".format(data_dict, e))

    def on_start_engagement(self, data_dict=None):
        # TODO: set listener to engagement events
        try:
            self.logger.info("Data received: {}".format(data_dict))
            start = data_dict["startEngagement"]
            if start is True:
                self.logger.info("Engagement is set.")
                self.on_engaged(True)
            else:
                self.logger.info("Engagement is disabled")
        except Exception as e:
            self.logger.error("Error while extracting engagement data: {} | {}".format(data_dict, e))

    def on_touch(self, data_dict=None):
        try:
            self.logger.info("Data received: {}".format(data_dict))
            # TODO
        except Exception as e:
            self.logger.error("Error while extracting touch data: {} | {}".format(data_dict, e))

    def on_speech_certainty(self, data_dict=None):
        try:
            self.logger.info("Data received: {}".format(data_dict))
            self.speech_handler.speech_certainty = data_dict["speechCertainty"]
        except Exception as e:
            self.logger.error("Error while extracting speech certainty data: {} | {}".format(data_dict, e))

    def on_voice_pitch(self, data_dict=None):
        try:
            self.logger.info("Data received: {}".format(data_dict))
            self.speech_handler.voice_pitch = data_dict["voicePitch"]
        except Exception as e:
            self.logger.error("Error while extracting voice pitch data: {} | {}".format(data_dict, e))

    def on_voice_speed(self, data_dict=None):
        try:
            self.logger.info("Data received: {}".format(data_dict))
            self.speech_handler.voice_speed = data_dict["voiceSpeed"]
        except Exception as e:
            self.logger.error("Error while extracting voice speed data: {} | {}".format(data_dict, e))

    def start_listening_to_db_stream(self):
        if self.db_change_thread is None:
            self.db_change_thread = DBChangeStreamThread()

            self.db_change_thread.add_data_observers(
                observers_dict={
                    "connectRobot": self.connect_robot,
                    "disconnectRobot": self.disconnect_robot,
                    "wakeUpRobot": self.on_wakeup,
                    "restRobot": self.on_rest,
                    "animateRobot": self.on_animate,
                    "interactionBlock": self.on_interaction_block,
                    "startInteraction": self.on_start_interaction,
                    "startEngagement": self.on_start_engagement,
                    "hideTabletImage": self.on_hide_tablet_image,
                    "speechCertainty": self.on_speech_certainty,
                    "voicePitch": self.on_voice_pitch,
                    "voiceSpeed": self.on_voice_speed
                }
            )

        self.db_change_thread.start_listening(self.db_helper.interaction_collection)
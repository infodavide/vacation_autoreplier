"""
Author: contact@infodavid.org
Automatically replies to messages both unread and unanswered.
This script has a rebound, self mail protection and also a rate limit protection.
You can adjust the time value with the variable block hours
Thanks to for original: https://gist.github.com/BertrandBordage/e07e5fe8191590daafafe0dbb05b5a7b

WARNING: This answers to any both unread and unanswered mail, even if it is years old.
         Don’t use on a mailbox with old messages left unread and unanswered.

Simply instantiate a ``AutoReplier`` and call the ``run`` method on an instance. Use a loop to run continuously the run method or use a cron task to execute the script.
Use (using Ctrl+C, typically) to stop the loop or until an error occurs, like a network failure.
"""
import base64
import os
import socket
import pathlib
import traceback
import logging
import sqlite3
import threading
import atexit
import signal
import re
import sys
import time
import locale
import datetime
import html
import xml.etree.ElementTree as etree
from enum import Enum
from typing import Any, Dict, List, cast, Pattern
from email import message_from_bytes, message
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid
from imaplib import IMAP4, IMAP4_SSL, ParseFlags
from logging.handlers import RotatingFileHandler
from os import execlp
from abc import ABC, abstractmethod
from smtplib import SMTP, SMTP_SSL
from subprocess import call
from textwrap import dedent
from time import sleep
from io import StringIO
from html.parser import HTMLParser
# not working with 3.9.2 on Debian from polyglot.detect import Detector

__author__ = 'David Rolland, contact@infodavid.org, based on script written by Bertrand Bordage'
__copyright__ = 'Copyright © 2022 David Rolland'
__license__ = 'MIT'

IMAP4_PORT: int = 143
SMTP_PORT: int = 25
DEFAULT_LANGUAGE: str = 'en'
DEFAULT_KEY: str = 'default'
IMAP_DATE_FORMAT: str = "%d-%b-%Y"
AUTOREPLIED_FLAG: str = 'AUTOREPLIED'


def create_rotating_log(path: str, level: str) -> logging.Logger:
    """
    Create the logger with file rotation.
    :param path: the path of the main log file
    :param level: the log level as defined in logging module
    :return: the logger
    """
    result: logging.Logger = logging.getLogger("AutoReplier")
    path_obj: pathlib.Path = pathlib.Path(path)
    if not os.path.exists(path_obj.parent.absolute()):
        os.makedirs(path_obj.parent.absolute())
    if os.path.exists(path):
        open(path, 'w').close()
    else:
        path_obj.touch()
    # noinspection Spellchecker
    formatter: logging.Formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    console_handler: logging.Handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    result.addHandler(console_handler)
    file_handler: logging.Handler = RotatingFileHandler(path, maxBytes=1024 * 1024 * 5, backupCount=5)
    # noinspection PyUnresolvedReferences
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    result.addHandler(file_handler)
    # noinspection PyUnresolvedReferences
    result.setLevel(level)
    return result


# noinspection PyTypeChecker
def get_message_language(value: message.Message) -> str:
    body: str = None
    if value.is_multipart():
        for part in value.walk():
            ctype: str = part.get_content_type()
            cdispo: str = str(part.get('Content-Disposition'))
            # skip any text/plain (txt) attachments
            if ctype == 'text/plain' and 'attachment' not in cdispo:
                body = html.escape(str(part.get_payload(decode=True), 'utf-8'))  # decode
                break
    # not multipart - i.e. plain text, no attachments, keeping fingers crossed
    else:
        ctype: str = value.get_content_type()
        if ctype != 'text/html':
            body = html.escape(str(value.get_payload(decode=True), 'utf-8'))
        else:
            body = str(value.get_payload(decode=True), 'utf-8')
    # Not working with 3.9.2 on Debian
    #if body is not None:
    #    for language in Detector(body).languages:
    #        if language.confidence > 85:
    #            return language.code
    return DEFAULT_LANGUAGE


class HTMLStripper(HTMLParser):
    @staticmethod
    def strip_tags(value) -> str:
        stripper: HTMLStripper = HTMLStripper()
        stripper.feed(value)
        return stripper.get_data()

    def __init__(self):
        super().__init__()
        self.reset()
        self.strict = False
        self.convert_charrefs = True
        self.text = StringIO()

    def handle_data(self, d) -> None:
        self.text.write(d)

    def get_data(self) -> str:
        return self.text.getvalue()


class ReplyTemplateType(str, Enum):
    HTML = 'HTML'
    TEXT = 'TEXT'


class ReplyTemplate(object):
    """
    Template used by the replier as reply.
    """
    lang: str = 'en'  # The language code used to link incoming message with the template
    type: ReplyTemplateType = ReplyTemplateType.TEXT
    email: str  # The email used to link incoming message with the template
    body: str = ''  # The content of the message

    def parse(self, node: etree.Element) -> None:
        """
        Parse the XML node
        """
        self.lang = node.get('lang')
        if not self.lang:
            self.lang = DEFAULT_LANGUAGE
        self.lang = self.lang.lower()
        self.type = node.get('type')
        if not self.type:
            self.type = ReplyTemplateType.TEXT
        self.email = node.get('email')
        self.body = node.text
        if not self.body:
            raise IOError('Template has no body')

    def __str__(self) -> str:
        buffer: str = 'Template'
        if self.lang is not None:
            buffer += ', language: ' + self.lang
        if self.type is not None:
            buffer += ', type: ' + str(self.type)
        if self.email is not None:
            buffer += ', email: ' + self.email
        if self.body is not None:
            buffer += '\n\tbody: ' + self.body
        return buffer


AddressList = List[str]
DomainList = List[str]
SubjectList = List[str]
TemplateList = List[ReplyTemplate]


class AutoReplierSettings(object):
    """
    Settings used by the replier.
    """
    date: datetime.datetime = None  # The expiration date
    refresh_delay: int = 300  # Check interval in seconds, use -1 to process one time without loop
    imap_server: str = None  # Full name or IP address of your IMAP server
    imap_use_ssl: bool = False  # Set True to use SSL
    imap_port: int = IMAP4_PORT  # Port of your IMAP server
    imap_user: str = None  # User used to connect to your IMAP server
    imap_password: str = None  # Password (base64 encoded) of the user used to connect to your IMAP server
    smtp_server: str = None  # Full name or IP address of your SMTP server
    smtp_use_ssl: bool = False  # Set True to use SSL
    smtp_port: int = SMTP_PORT  # Port of your SMTP server
    smtp_user: str = None  # User used to connect to your SMTP server
    smtp_password: str = None  # Password (base64 encoded) of the user used to connect to your SMTP server
    block_hours: int = 12  # Number of hours used to block incoming email address
    skipped_addresses: AddressList = list()  # List of email addresses (or regular expressions) used to ignore incoming message
    skipped_domains: DomainList = list()  # List of domains (or regular expressions)  to ignore incoming message
    skipped_subjects: SubjectList = list()  # List of subjects (or regular expressions)  to ignore incoming message
    templates: TemplateList = list()  # List of reply templates
    path: str = None  # Path for the files used by the application
    db_path: str = 'autoreplier.db'
    log_path: str  # Path to the logs file, not used in this version
    log_level: str  # Level of logs, not used in this version

    def parse(self, path: str) -> None:
        """
        Parse the XML configuration.
        """
        with open(path) as f:
            tree = etree.parse(f)
        root_node: etree.Element = tree.getroot()
        v = root_node.get('refresh-delay')
        if v is not None:
            self.refresh_delay = int(v)
        else:
            self.refresh_delay = 300
        v = root_node.get('date')
        if v is not None:
            self.date = datetime.datetime.strptime(v, '%Y-%m-%d')
        else:
            raise IOError('No date attribute specified in the XML configuration, refer to the autoreplier.xsd')
        v = root_node.get('block-hours')
        if v is not None:
            self.block_hours = int(v)
        else:
            self.block_hours = 12
        log_node: etree.Element = root_node.find('log')
        if log_node is not None:
            v = log_node.find('path')
            if v is not None:
                self.log_path = v.text
            v = log_node.find('level')
            if v is not None:
                self.log_level = v.text
        accounts = {}
        for node in tree.findall('accounts/account'):
            v1 = node.get('user')
            v2 = node.get('password')
            v3 = node.get('id')
            if v1 is not None and v2 is not None and v3 is not None:
                accounts[v3] = [v1, v2]
        imap_node: etree.Element = root_node.find('imap')
        if imap_node is not None:
            self.imap_server = imap_node.get('server')
            v = imap_node.get('port')
            if v is not None:
                self.imap_port = int(v)
            else:
                self.imap_port = 143
            self.imap_use_ssl = imap_node.get('ssl') == 'True' or imap_node.get('ssl') == 'true'
        else:
            raise IOError('No imap element specified in the XML configuration, refer to the autoreplier.xsd')
        account_id: str = imap_node.get('account-id')
        account = accounts[account_id]
        if account:
            self.imap_user = account[0]
            self.imap_password = account[1]
        smtp_node: etree.Element = root_node.find('smtp')
        if smtp_node is not None:
            self.smtp_server = smtp_node.get('server')
            v = smtp_node.get('port')
            if v is not None:
                self.smtp_port = int(v)
            else:
                self.smtp_port = 25
            self.smtp_use_ssl = smtp_node.get('ssl') == 'True' or smtp_node.get('ssl') == 'true'
        else:
            raise IOError('No smtp element specified in the XML configuration, refer to the autoreplier.xsd')
        account_id = smtp_node.get('account-id')
        account = accounts[account_id]
        if account:
            self.smtp_user = account[0]
            self.smtp_password = account[1]
        for node in tree.findall('skipped/domains/domain'):
            self.skipped_domains.append(node.text)
        for node in tree.findall('skipped/addresses/address'):
            self.skipped_addresses.append(node.text)
        for node in tree.findall('skipped/subjects/subject'):
            self.skipped_subjects.append(node.text)
        for node in tree.findall('templates/template'):
            template: ReplyTemplate = ReplyTemplate()
            template.parse(node)
            self.templates.append(template)
        self.path = os.path.dirname(path)


class AutoReplier:
    """
    Read your unread and unanswered messages and reply automatically if a template is available using the same address as the recipient of the incoming message.
    """
    __logger: logging.Logger = None
    __settings: AutoReplierSettings = None
    __imap: IMAP4 = None
    __smtp: SMTP = None
    __active: bool = False
    __test: bool = False
    __rate_limit: int = 2
    __age_in_days: int = 1
    __login_retry_delay: int = 15
    __login_retries: int = 10
    __html_templates: Dict[str, Dict[str, ReplyTemplate]] = {}  # List of reply templates in HTML by address and language
    __text_templates: Dict[str, Dict[str, ReplyTemplate]] = {}  # List of reply templates in plain text by address and language
    __skipped_addresses: List = list()  # List of texts or patterns describing the addresses to ignore
    __skipped_domains: List = list()  # List of texts or patterns describing the domains to ignore
    __skipped_subjects: List = list()  # List of texts or patterns describing the subjects to ignore

    def __init__(self, settings: AutoReplierSettings, logger: logging.Logger):
        self.__settings = settings
        self.__logger = logger
        self.__logger.info('Initializing ' + self.__class__.__name__ + '...')
        # Status flags
        self.__active = False
        # Locks
        self.__lock: threading.RLock = threading.RLock()
        self.__start_lock: threading.RLock = threading.RLock()
        self.__stop_lock: threading.RLock = threading.RLock()
        # Hooks
        atexit.register(self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self._initialize()
        self._login()

    # noinspection PyTypeChecker
    def _initialize(self) -> None:
        """
        Do some preprocessing tasks.
        """
        for template in self.__settings.templates:
            if template.body is not None and template.lang is not None:
                if template.lang == 'en':
                    new_locale = 'en_US.utf8'
                else:
                    new_locale = template.lang.lower() + '_' + template.lang.upper() + '.utf8'
                if self.__logger.isEnabledFor(logging.DEBUG):
                    self.__logger.debug('Locale for template: ' + new_locale)
                previous_locale: str = locale.getlocale(locale.LC_TIME)
                locale.setlocale(locale.LC_TIME, new_locale)
                template.body = template.body.replace('${date}', self.__settings.date.strftime("%A %-d %B %Y"))
                locale.setlocale(locale.LC_TIME, previous_locale)
            #HTML or text template ?
            d1: Dict[str, Dict[str, ReplyTemplate]] = self.__text_templates
            if template.type == ReplyTemplateType.HTML:
                d1 = self.__html_templates
            #Set default if not already done
            if DEFAULT_KEY in d1:
                d2: Dict[str, ReplyTemplate] = d1[DEFAULT_KEY]
            else:
                d2: Dict[str, ReplyTemplate] = {}
                d1[DEFAULT_KEY] = d2
            if len(d2) == 0:
                d2[DEFAULT_KEY] = template
                if self.__logger.isEnabledFor(logging.DEBUG):
                    self.__logger.debug('Template added to default: ' + str(template))
            #Set using email
            if template.email:
                if template.email in d1:
                    d2: Dict[str, ReplyTemplate] = d1[template.email]
                else:
                    d2: Dict[str, ReplyTemplate] = {}
                    d1[template.email] = d2
            else:
                d2 = d1[DEFAULT_KEY]
            #Set using language
            if template.lang:
                if template.lang not in d2:
                    d2[template.lang] = template
                    if self.__logger.isEnabledFor(logging.DEBUG):
                        self.__logger.debug('Template added to ' + template.lang + ': ' + str(template))
            else:
                d2[DEFAULT_KEY] = template
        for value in self.__settings.skipped_addresses:
            try:
                self.__skipped_addresses.append(re.compile(value, flags=0))
            except re.error as ex:
                exc_type4, exc_value4, exc_traceback4 = sys.exc_info()
                traceback.print_tb(exc_traceback4, limit=6, file=sys.stderr)
                self.__logger.error(ex)
                self.__skipped_addresses.append(value)
        for value in self.__settings.skipped_domains:
            try:
                self.__skipped_domains.append(re.compile(value, flags=0))
            except re.error as ex:
                exc_type4, exc_value4, exc_traceback4 = sys.exc_info()
                traceback.print_tb(exc_traceback4, limit=6, file=sys.stderr)
                self.__logger.error(ex)
                self.__skipped_domains.append(value)
        for value in self.__settings.skipped_subjects:
            try:
                self.__skipped_subjects.append(re.compile(value, flags=0))
            except re.error as ex:
                exc_type4, exc_value4, exc_traceback4 = sys.exc_info()
                traceback.print_tb(exc_traceback4, limit=6, file=sys.stderr)
                self.__logger.error(ex)
                self.__skipped_subjects.append(value)

    def _login(self) -> None:
        """
        Login on the IMAP and SMTP servers.
        """
        self.__logger.info('Login...')
        retry: int = self.__login_retries
        while retry > 0:
            try:
                if self.__settings.imap_use_ssl:
                    if self.__logger.isEnabledFor(logging.DEBUG):
                        self.__logger.debug('Using IMAP4 SSL and server: ' + self.__settings.imap_server + ' and port: ' + str(self.__settings.imap_port))
                    self.__imap = IMAP4_SSL(self.__settings.imap_server, self.__settings.imap_port)
                else:
                    if self.__logger.isEnabledFor(logging.DEBUG):
                        self.__logger.debug('Using IMAP4 and server: ' + self.__settings.imap_server + ' and port: ' + str(self.__settings.imap_port))
                    self.__imap = IMAP4(self.__settings.imap_server, self.__settings.imap_port)
                v: str = base64.b64decode(self.__settings.imap_password).decode('utf8')
                self.__logger.info('IMAP4 login using user: ' + self.__settings.imap_user + ' and password: ' + re.sub('.', '*', v) + '...')
                self.__imap.login(self.__settings.imap_user, v)
                if self.__settings.smtp_use_ssl:
                    if self.__logger.isEnabledFor(logging.DEBUG):
                        self.__logger.debug('Using SMTP SSL and server: ' + self.__settings.smtp_server + ' and port: ' + str(self.__settings.smtp_port))
                    self.__smtp = SMTP_SSL(self.__settings.smtp_server, self.__settings.smtp_port)
                else:
                    if self.__logger.isEnabledFor(logging.DEBUG):
                        self.__logger.debug('Using SMTP and server: ' + self.__settings.smtp_server + ' and port: ' + str(self.__settings.smtp_port))
                    self.__smtp = SMTP(self.__settings.smtp_server, self.__settings.smtp_port)
                v = base64.b64decode(self.__settings.smtp_password).decode('utf8')
                self.__logger.info('SMTP login using user: ' + self.__settings.smtp_user + ' and password: ' + re.sub('.', '*', v) + '...')
                self.__smtp.login(self.__settings.smtp_user, v)
                self.__logger.info('Login done')
                retry = 0
            except socket.gaierror as ex:
                retry = retry - 1
                if retry <= 0:
                    raise ex
                else:
                    self.__logger.warning('Login failed, retrying in ' + str(self.__login_retry_delay) + 's')
                    sleep(self.__login_retry_delay)

    def close(self) -> None:
        """
        Close the IMAP and SMTP connections.
        """
        self.__logger.info('Closing...')
        if self.__smtp:
            self.__logger.debug('Closing SMTP connection...')
            self.__smtp.close()
        if self.__imap:
            self.__logger.debug('Closing IMAP4 connection...')
            self.__imap.logout()
        self.__logger.info('Closing done')

    def _is_skipped(self, original: message.Message):
        sender_full: str = original['Reply-To'] or original['From']
        sender: str = sender_full
        if '<' in sender:
            sender = (sender.split('<'))[1].split('>')[0]
        self.__logger.info('Incoming message from ' + sender + ' (' + original['Subject'] + '). Checking history....')
        # Check if sender address is ignored
        for value in self.__skipped_addresses:
            if isinstance(value, Pattern):
                if value.match(sender):
                    self.__logger.info('Mail from ' + sender + ' is rejected by address filter: ' + value.pattern)
                    return True
            elif value == sender:
                return True
        # Check if sender domain is ignored
        domain: str = sender.split('@')[1]
        if self.__logger.isEnabledFor(logging.DEBUG):
            self.__logger.debug('Domain: ' + domain)
        for value in self.__skipped_domains:
            if isinstance(value, Pattern):
                if value.match(domain):
                    self.__logger.info('Mail from ' + sender + ' is rejected by domain filter: ' + value.pattern)
                    return True
            elif value == domain:
                return True
        # Check if subject is ignored
        subject: str = original['Subject']
        if self.__logger.isEnabledFor(logging.DEBUG):
            self.__logger.debug('Subject: ' + subject)
        for value in self.__skipped_subjects:
            if isinstance(value, Pattern):
                if value.match(domain):
                    self.__logger.info('Mail from ' + sender + ' is rejected by subject filter: ' + value.pattern + " and subject: " + subject)
                    return True
            elif value == subject:
                return True
        # Check for recent incoming mails from this address
        con: sqlite3.Connection = self._db_connect()
        skipped: bool = False
        now: datetime.datetime = datetime.datetime.now()
        try:
            cur: sqlite3.Cursor = con.cursor()
            if self.__logger.isEnabledFor(logging.DEBUG):
                for row in cur.execute("SELECT count(id) FROM senders"):
                    self.__logger.debug('Entries in table: %s' % (str(row[0])))
            for row in cur.execute("SELECT id,date FROM senders WHERE mail=?", (sender,)):
                break_date = now - datetime.timedelta(hours=self.__settings.block_hours)
                then = datetime.datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S.%f")
                self.__logger.info('Found ' + sender + ' at ' + str(row[1]) + ' - ID ' + str(row[0]))
                if then < break_date:  # If older: Delete
                    if self.__logger.isEnabledFor(logging.DEBUG):
                        self.__logger.debug('Last entry ' + str(row[0]) + ' from ' + sender + ' is old. Delete...')
                    cur.execute("DELETE FROM senders WHERE id=?", (str(row[0])))
                elif then >= break_date:  # If Recent: Reject
                    self.__logger.debug('Recent entry found. Not sending any mail')
                    skipped = True
            if skipped:
                return skipped
            # Accept
            self.__logger.info('Memorizing ' + sender)
            cur.execute("INSERT INTO senders (mail, date) values (?, ?)", (sender, now))
            con.commit()
        except Exception as ex:
            exc_type4, exc_value4, exc_traceback4 = sys.exc_info()
            traceback.print_tb(exc_traceback4, limit=6, file=sys.stderr)
            self.__logger.error(ex)
        finally:
            if con:
                con.close()
        return skipped

    def _db_connect(self) -> sqlite3.Connection:
        """
        Connect to the SQLITE3 database.
        """
        self.__logger.info('Connecting to the database: %s...' %  self.__settings.db_path)
        con: sqlite3.Connection = sqlite3.connect( self.__settings.db_path, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
        # self.__db_con.set_trace_callback(print)
        self.__logger.info('Connected to the database')
        return con

    def _create_table(self) -> None:
        """
        Create the table if it does not exist.
        """
        self.__logger.info('Creating table if not present in the database...')
        if self.__test and os.path.exists(self.__settings.db_path):
            os.unlink(self.__settings.db_path)
        con: sqlite3.Connection = self._db_connect()
        try:
            cur: sqlite3.Cursor = con.cursor()
            cur.execute('''CREATE TABLE IF NOT EXISTS senders (id INTEGER PRIMARY KEY, mail text, date datetime)''')
            con.commit()
            if self.__logger.isEnabledFor(logging.DEBUG):
                for row in cur.execute("SELECT count(id) FROM senders"):
                    self.__logger.debug('Entries in table: %s' % (str(row[0])))
        except Exception as ex:
            exc_type4, exc_value4, exc_traceback4 = sys.exc_info()
            traceback.print_tb(exc_traceback4, limit=6, file=sys.stderr)
            self.__logger.error(ex)
        finally:
            if con:
                con.close()
        self.__logger.info('Table created')

    # noinspection PyTypeChecker
    def _create_auto_reply(self, original: message.Message):
        """
        Create the message
        :param original: original message
        """
        original_recipient: str = original['To']
        if '<' in original_recipient:
            original_recipient: str = (original_recipient.split('<'))[1].split('>')[0]
        original_language: str = original['Content-Language']
        if original_language is None:
            original_language = get_message_language(original)
            if original_language is None:
                original_language = DEFAULT_LANGUAGE
        original_language = original_language.split(',')[0]
        if self.__logger.isEnabledFor(logging.DEBUG):
            self.__logger.debug('Original language: ' + original_language)
        mail: MIMEMultipart = MIMEMultipart('alternative')
        mail['Message-ID'] = make_msgid()
        mail['References'] = mail['In-Reply-To'] = original['Message-ID']
        mail['Subject'] = 'Re: ' + original['Subject']
        mail['From'] = original_recipient
        mail['To'] = original['Reply-To'] or original['From']
        if self.__logger.isEnabledFor(logging.DEBUG):
            self.__logger.debug('Original recipient: ' + original_recipient)
        template: ReplyTemplate = None
        # Search in text templates
        d1: Dict[str, Dict[str, ReplyTemplate]] = self.__text_templates
        if original_recipient in d1:
            d2: Dict[str, ReplyTemplate] = d1[original_recipient]
            if original_language in d2:
                template = d2[original_language]
            elif DEFAULT_KEY in d2:
                template = d2[DEFAULT_KEY]
        elif DEFAULT_KEY in d1:
            d2: Dict[str, ReplyTemplate] = d1[DEFAULT_KEY]
            if DEFAULT_KEY in d2:
                template = d2[DEFAULT_KEY]
        if template:
            if template.lang:
                mail['Content-Language'] = template.lang
            mail.attach(MIMEText(dedent(template.body), 'plain'))
            if self.__logger.isEnabledFor(logging.DEBUG):
                self.__logger.debug('Using text plain template:\n' + template.body)
        # Search in HTML templates
        template = None
        d1: Dict[str, Dict[str, ReplyTemplate]] = self.__html_templates
        if original_recipient in d1:
            d2: Dict[str, ReplyTemplate] = d1[original_recipient]
            if original_language in d2:
                template = d2[original_language]
            elif DEFAULT_KEY in d2:
                template = d2[DEFAULT_KEY]
        elif DEFAULT_KEY in d1:
            d2: Dict[str, ReplyTemplate] = d1[DEFAULT_KEY]
            if DEFAULT_KEY in d2:
                template = d2[DEFAULT_KEY]
        if template:
            if template.lang:
                mail['Content-Language'] = template.lang
            mail.attach(MIMEText(template.body, 'html'))
            if self.__logger.isEnabledFor(logging.DEBUG):
                self.__logger.debug('Using HTML template:\n' + template.body)
        if len(mail.items()) > 0:
            return mail
        return None

    # noinspection PyBroadException
    def _send_auto_reply(self, original: message.Message):
        """
        Check the sender and if not ignored, reply to the message
        :param mail_id: identifier of the message
        """
        # Check if address has been used 12h or if it must be ignored
        if self._is_skipped(original):
            self.__logger.info('Mail from "' + original['From'] + '" will be ignored')
            return
        # Send with Rate limit & error prevention
        success = False
        i: int = 0
        while not success and i < 5:
            i = i + 1
            try:
                reply: message = self._create_auto_reply(original)
                success = True
                if self.__test:
                    self.__logger.info('Test mode activated, reply will not be sent')
                elif reply:
                    self.__smtp.sendmail(original['To'], [original['From']], reply.as_bytes())
                else:
                    self.__logger.warning('No template available')
                    success = False
                if success:
                    self.__logger.info('Replied to "' + original['From'] + '" for the mail "' + original['Subject'] + '"')
            except Exception:
                exc_type, exc_value, exc_traceback = sys.exc_info()
                traceback.print_tb(exc_traceback, limit=6, file=sys.stderr)
                self.__logger.warning('Error on send (rate limit?). Wait 30s and reconnect....')
                self.close()
                time.sleep(30)
                self._login()

    def _reply(self, mail_id: str) -> None:
        """
        Reply to the message using its identifier.
        :param mail_id: identifier of the message
        """
        try:
            self.__imap.select(readonly=False)
            _, data = self.__imap.fetch(mail_id, '(RFC822)')
            flags = list()
            for flag in ParseFlags(data[0][0]):
                flags.append(flag.decode())
            if self.__test:
                self.__logger.info('Test mode activated, incoming message will not be marked as answered')
            else:
                self.__imap.store(mail_id, '+FLAGS', AUTOREPLIED_FLAG)
                self.__imap.store(mail_id, '-FLAGS', '\\SEEN')
                self.__logger.info('%s flag added to the message.' % AUTOREPLIED_FLAG)
            flags_str: str = ' '.join(flags)
            if self.__logger.isEnabledFor(logging.DEBUG):
                self.__logger.debug('Flags: %s' % flags_str)
            if AUTOREPLIED_FLAG in flags_str:
                self.__logger.warning('Message already has the %s flags' % AUTOREPLIED_FLAG)
                return
        finally:
            self.__imap.close()
        self._send_auto_reply(message_from_bytes(data[0][1]))

    def _check_mails(self) -> None:
        """
        Check incoming unseen and unanswered messages.
        """
        since_date: datetime.datetime = (datetime.datetime.today() - datetime.timedelta(days=self.__age_in_days))
        if self.__logger.isEnabledFor(logging.DEBUG):
            self.__logger.debug('Searching messages using: SINCE "%s" UNSEEN UNANSWERED UNKEYWORD %s' % (since_date.strftime(IMAP_DATE_FORMAT), AUTOREPLIED_FLAG))
        try:
            self.__imap.select(readonly=False)
            _, data = self.__imap.search(None, '(SINCE "%s" UNSEEN UNANSWERED UNKEYWORD %s)' % (since_date.strftime(IMAP_DATE_FORMAT), AUTOREPLIED_FLAG))
        finally:
            self.__imap.close()
        for mail_id in data[0].split():
            self._reply(mail_id)
            time.sleep(self.__rate_limit)  # Rate Limit prevention
        self.__logger.debug('Search done')

    def is_running(self) -> bool:
        """
        Check if running.
        :return: True if running
        """
        return self.__active

    def stop(self) -> None:
        """
        Stop the process.
        """
        with self.__lock:
            if not self.__active:
                return
        with self.__stop_lock:
            self.__active = False

    def start(self) -> None:
        """
        Start the process.
        """
        with self.__lock:
            if self.__active:
                return
        with self.__start_lock:
            try:
                self._create_table()
                self.__logger.info('Now listening... Blocking rebounds for ' + str(self.__settings.block_hours) + ' hours')
                self.__active = True
                if datetime.datetime.now() >= self.__settings.date:
                    self.__logger.info('Date passed... stopping')
                    return
                self._check_mails()
                if self.__settings.refresh_delay > 0:
                    sleep(self.__settings.refresh_delay)
                    while self.__active:
                        self._check_mails()
                        sleep(self.__settings.refresh_delay)
            finally:
                self.close()

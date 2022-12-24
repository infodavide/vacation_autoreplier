# Vacation AutoReplier 
Python script to reply to incoming messages during vacation perdiod.


### Installation

Copy the following files to a dedicated directory:
- autoreplier.py
- autoreplier.xsd (used to help you when writing the XML configuration file)
- autoreplier_configuration_sample.xml (to get an example of configuration)
- custom_autoreplier.py (to launch the script using the XML configuration file)

You can use a shell file to launch the script in the your crontab.


### Configuration

The XML configuration file looks like this.

    <configuration block-hours="12" refresh-delay="300" date="2050-01-01" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
        xsi:noNamespaceSchemaLocation="autoreplier.xsd" >
        <accounts>
            <account id="id1" user="login@domain.com" password="secret" />
        </accounts>
        <imap server="imap.domain.com" port="993" ssl="True" account-id="id1" />
        <smtp server="smtp.domain.com" port="465" ssl="True" account-id="id1" />
        <skipped>
            <domains>
               <domain>linkedin.com</domain>
                <domain>domain.com</domain>
            </domains>
            <addresses>
                <address>jenkins.*</address>
                <address>alert@.*</address>
                <address>noreply.*</address>
                <address>no-reply.*</address>
                <address>no_reply.*</address>
            </addresses>
            <subjects>
                <subject>Jenkins build is.*</subject>
            </subjects>
        </skipped>
        <templates>
        <template lang="it" type="HTML">
    <![CDATA[
    <p>Salve,</p>
    <p>
        Sono via fino al ${date}.<br />
        Avrete una risposta quando tornerò.<br />
    </p>
    <p>Buona giornata</p>
    ]]>
        </template>
    </configuration>


**Parameters:**
- **block-hours**: used to block sender and avoid replying to it during the specified duration in hours. By default, value is 12.
- **refresh-delay**: used when running the script without crontab. If value is negative, the check of messages is done one time. Otherwise, the checks are done in a loop with a refresh delay specified in seconds.
- **date**: used to provide the end date of the replier. When date is reached, the check of message are skipped. To use the date in a template, you can write ${date}.

In accounts, you can specify one or more accounts with an identifier (used to refer to it), a username and a password in base 64.
For IMAP and SMTP, you have to specify the server IP or name, the port, the identifier of the associated account and the boolean flag ssl to indicate if a SSL connection is required. 

In skipped domains, addresses and subjects, you can specify values or regular expressions to avoid replying to messages having one of the given domain, address or subject.

In templates, you can write your replies for HTML or plain text contents. The type is used to set the content type of the reply and the language is used to select the reply having the same language as the incoming message. The default templates are picked using the order of the sequence.


### Execution

#### setup and customization
You can change the custom_autoreplier.py to instantiate the configuration object and make the full setup using python or by loading the XML configuration.
Here is a basic example:
    #!/usr/bin/python
    # -*- coding: utf-*-
    
    import argparse
    import os
    import pathlib
    import sys
    import traceback
    from filelock import FileLock
    from autoreplier import AutoReplier, AutoReplierSettings, create_rotating_log
    
    parser = argparse.ArgumentParser(prog='Autoreplier', description='Tool used to reply to incoming messages')
    parser.add_argument('-l', help='Log level', default='INFO')
    parser.add_argument('-f', required=True, help='Configuration file')
    args = parser.parse_args()
    
    LOG_LEVEL: str = args.l
    if LOG_LEVEL.startswith('"') and LOG_LEVEL.endswith('"'):
        LOG_LEVEL = LOG_LEVEL[1:-1]
    if LOG_LEVEL.startswith("'") and LOG_LEVEL.endswith("'"):
        LOG_LEVEL = LOG_LEVEL[1:-1]
    CONFIG_PATH: str = args.f
    if CONFIG_PATH.startswith('"') and CONFIG_PATH.endswith('"'):
        CONFIG_PATH = CONFIG_PATH[1:-1]
    if CONFIG_PATH.startswith("'") and CONFIG_PATH.endswith("'"):
        CONFIG_PATH = CONFIG_PATH[1:-1]
    if not os.path.exists(CONFIG_PATH):
        CONFIG_PATH = str(pathlib.Path(__file__).parent) + os.sep + CONFIG_PATH
    LOG_PATH: str = os.path.splitext(CONFIG_PATH)[0] + '.log'
    LOCK_PATH: str = os.path.splitext(CONFIG_PATH)[0] + '.lck'
    settings = AutoReplierSettings()
    settings.parse(CONFIG_PATH)
    
    
    if __name__ == '__main__':
        with FileLock(LOCK_PATH):
            try:
                AutoReplier(settings, create_rotating_log(LOG_PATH, LOG_LEVEL)).start()
            except KeyboardInterrupt:
                exc_type, exc_value, exc_traceback = sys.exc_info()
                traceback.print_tb(exc_traceback, limit=6, file=sys.stderr)
                exit()

#### crontab on linux systems
You can use the crontab to execute the script by refering to a shell file ike this one:

    #!/bin/bash
    DIR=~/bin/vacation_autoreplier
    python3.7 $DIR/custom_autoreplier.py -f $DIR/autoreplier.xml>/dev/null
    exit 0

You can edit the crontab with 'crontab -e' and add the following line:

    */5 * * * * /home/davide/bin/vacation_autoreplier.sh
to execute the custom shell script every 5 minutes.

#### systemd for linux systems
You can use a systemd service to start the script on boot.

    [Unit]
    Description=Vacation AutoReplier Service
    After=network.target
    
    [Service]
    Type=idle
    Restart=on-failure
    User=root
    ExecStart=/usr/bin/python3.7 /opt/autoreplier/custom_autoreplier.py -f /opt/autoreplier/autoreplier.xml>/dev/null
    
    [Install]
    WantedBy=multi-user.target

#### windows systems
On Ms-Windows, you can use the scheduled tasks manager and refer to the custom python script like mentioned in the linux section. 
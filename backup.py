from datetime import datetime, timezone
import json
import os
import re
import shutil
import subprocess

# dry run option for development
DRY_RUN = False

def is_linux():
    return os.name == 'posix' and 'linux' in os.uname().sysname.lower()

script_directory = os.path.dirname(os.path.abspath(__file__))

class Config:
    def __init__(self, config_path='config.json'):
        try:
            with open(config_path) as f:
                self._config = json.load(f)
        except FileNotFoundError:
            print(f'{config_path} does not exist')
            print(f'copy config.example.json to {config_path} and fill in the values to get started')
            exit(1)
        
        self._tokens = {}  # key is token name, value is token value
        for token in self._config['tokens']:
            if 'name' not in token or 'value' not in token:
                print(f'Token must have "name" and "value" fields defined - found fields {token.keys()}')
                exit(1)
            self._tokens[token['name']] = token['value']

        guilds = []
        for guild in self._config['guilds']:
            if 'enabled' in guild and not guild['enabled']:
                continue
            self.validate_guild(guild)
            guild['tokenValue'] = self._tokens[guild['tokenName']]
            if guild['guildId'] == '@me':
                guild['type'] = 'exportdm'
            elif guild['guildId'] == 'channel':
                guild['type'] = 'export'
            else:
                guild['type'] = 'exportguild'

            if 'throttleHours' not in guild:
                guild['throttleHours'] = 0

            guilds.append(guild)

        self.guilds = guilds

        if 'cliPath' not in self._config['dce']:
            print(f'DCE CLI Path not configured')
            exit(1)
        else:
            self.clipath = self._config['dce']['cliPath']

        if 'outputPath' not in self._config['dce']:
            print(f'Output Path not configured')
            exit(1)
        else:
            self.outputpath = self._config['dce']['outputPath']

        if 'parallel' not in self._config['dce']:
            print(f'Parallel not configured')
            exit(1)
        else:
            self.parallel = self._config['dce']['parallel']

        if 'locale' not in self._config['dce']:
            print(f'Locale not configured')
            exit(1)
        else:
            self.locale = self._config['dce']['locale']


    def validate_guild(self, guild) -> None:
        """
        print helpful error messages if guild config is not valid
        is not validated against the actual discord API, just basic checks
        """
        invalid_path_chars = re.compile(r'[<>:"/\\|?*]')
        discord_snowflake = re.compile(r'^\d{17,19}$')  # 19 digits won't be enough in 2090. But you probably won't be using this script then
        required_fields = ['tokenName', 'guildId', 'guildName']

        for required_field in required_fields:
            if required_field not in guild:
                print(f'Guild must have "{required_field}" field defined - found fields {guild.keys()}')
                exit(1)
            if type(guild[required_field]) != str:
                print(f'Guild field "{required_field}" must be a string - found {type(guild[required_field])}')
                exit(1)
            if guild[required_field] == "":
                print(f'Guild must have "{required_field}" field defined - found empty value')
                exit(1)

        if guild['guildId'] != '@me' and not 'channel' and not discord_snowflake.match(guild['guildId']):
            print(f'Guild field "guildId" must be a discord snowflake (must be a string of 17-19 digits or "@me" for DMs) - found {guild["guildId"]}')
            exit(1)

        if invalid_path_chars.search(guild['guildName']):
            print(f'Guild field "guildName" must not contain invalid path characters (must be a non-empty string without any of <>:"/\\|?* - because it is used as a folder name) - found {guild["guildName"]}')
            exit(1)

        if "enabled" in guild and type(guild["enabled"]) != bool:
            print(f'Optional guild field "enabled" must be a boolean if set - found {type(guild["enabled"])}')
            exit(1)

        if guild['tokenName'] not in self._tokens:
            print(self._tokens)
            print(f'Token "{guild["tokenName"]}" not found in tokens. Available tokens: {", ".join(self._tokens.keys())}')
            exit(1)


class Timestamps:
    def __init__(self, timestamp_path='/out/metadata.json'):
        self._timestampsGuilds = {}
        self._timestamp_path = timestamp_path

        try:
            with open(timestamp_path, encoding='utf-8') as f:
                metadata = json.load(f)
                if 'lastExportsTimestamps' in metadata:
                    self._timestampsGuilds = metadata['lastExportsTimestamps']

        except FileNotFoundError:
            print('/out/metadata.json does not exist, starting from scratch')
            with open(timestamp_path, 'w', encoding='utf-8') as f:
                json.dump({'lastExportsTimestamps': {}}, f)

    def get_timestamp(self, guildId) -> str:
        return self._timestampsGuilds.get(guildId, None)

    def set_timestamp(self, guildId, timestamp) -> None:
        self._timestampsGuilds[guildId] = timestamp
        with open(self._timestamp_path, 'r', encoding='utf-8') as f:
            json_content = json.load(f)

        with open(self._timestamp_path, 'w', encoding='utf-8') as f:
            json_content['lastExportsTimestamps'] = self._timestampsGuilds
            json.dump(json_content, f, indent=2)


class CommandRunner:
    def __init__(self, config: Config, timestamps: Timestamps) -> None:
        self.config = config
        self.timestamps = timestamps

    def redact_dce_command(self, dce_command) -> str:
        """
        returns redacted discord token in command to safely print them to the console
        """
        dce_command = re.sub(r'--token "(.{5})[^"]+"', r'--token "\1***"', dce_command)
        return dce_command

    def export(self) -> None:
        for guild in self.config.guilds:
            if guild["channelId"]: 
                print(f'ChannelId {guild["guildName"]} ({guild["channelId"]}):')
            else:
                print(f'Guild {guild["guildName"]} ({guild["guildId"]}):')
                
            # export may take a long time. We want to know when the export started, so the next export won't miss any new messages created during the export
            nowTimestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")                   # example 2023-08-26T02:46:30.229228Z
            nowTimestampFolder = re.sub(r'\.\d+Z', '', nowTimestamp.replace(':', '-').replace('T', '--'))  # example 2023-08-26--02-46-30
            last_export_timestamp = self.timestamps.get_timestamp(guild['channelId']) if 'channelId' in guild else self.timestamps.get_timestamp(guild['guildId'])

            # skip export if export was done recently (based on throttleHours from config)
            if last_export_timestamp is not None:
                last_export_timestamp = last_export_timestamp.replace('Z', '+00:00')
                nowTimestamp = nowTimestamp.replace('Z', '+00:00')
                hoursSinceLastExport = (datetime.fromisoformat(nowTimestamp) - datetime.fromisoformat(last_export_timestamp)).total_seconds() / 3600
                print(f'  Last export was {hoursSinceLastExport:.2f} hours ago')
                if hoursSinceLastExport < guild['throttleHours']:
                    print(f'  Skipping export because throttleHours is set to {guild["throttleHours"]} hours')
                    continue

            if os.path.exists(self.config.clipath):
                dce_path = self.config.clipath
                common_args = f'--format Json --media --reuse-media --fuck-russia --markdown false --parallel {self.config.parallel} --locale {self.config.locale}'
                custom_args = f'--token "{guild["tokenValue"]}" --media-dir "{self.config.outputpath}/{guild["guildName"]}/_media/" --output "{self.config.outputpath}/{guild["guildName"]}/{nowTimestampFolder}/"'
                if "after" in guild:
                    custom_args += f' --after "{guild["after"]}"'
                if "before" in guild:
                    custom_args += f' --before "{guild["before"]}"'
                if "partition" in guild:
                    custom_args += f' --partition "{guild["partition"]}"'
            else:
                print("#########################################################################################")
                print('# DiscordChatExporter.Cli not found!                                             #')
                print('#   Ensure path is correctly defined in config.json')
                print("#########################################################################################")
                exit(1)


            if guild['type'] == 'exportguild':
                command = f"{dce_path} exportguild --guild {guild['guildId']} --include-threads All {common_args} {custom_args}"
            elif guild['type'] == 'exportdm':
                command = f"{dce_path} exportdm {common_args} {custom_args}"
            elif guild['type'] == 'export':
                command = f"{dce_path} export --channel {guild['channelId']} {common_args} {custom_args}"
            else:
                print(f'  Unknown export type {guild["type"]}')
                exit(1)

            if last_export_timestamp is not None:
                command = f'{command} --after "{last_export_timestamp}"'

            print(f"  {self.redact_dce_command(command)}")

            if not DRY_RUN:
                proc = subprocess.run(command, shell=True)

                return_code = proc.returncode
                print(f"  return code {return_code}")

                if return_code == 0:
                    if 'channelId' in guild:
                        self.timestamps.set_timestamp(guild['channelId'], nowTimestamp)
                    else:
                        self.timestamps.set_timestamp(guild['guildId'], nowTimestamp)
                else:
                    print(f'  Error exporting {guild["guildName"]}. Does {script_directory}/dce/DiscordChatExporter.Cli exist? Maybe there are no new messages? Check the logs above for more information.')

            else:
                print("  dry run, not really running the command and not updating timestamps")




def main():
    timestamps = Timestamps(timestamp_path='/out/metadata.json')
    config = Config(config_path='/out/config.json')
    command_runner = CommandRunner(config=config, timestamps=timestamps)
    command_runner.export()


if __name__ == '__main__':
    main()

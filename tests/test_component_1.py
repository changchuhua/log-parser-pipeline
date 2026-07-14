import unittest
from unittest.mock import patch, mock_open
import pandas as pd
import json
from component_1_dataset_gen.transform_to_ecs import process_loghub, process_botsv3

class TestComponent1(unittest.TestCase):
    @patch('pandas.read_csv')
    @patch('builtins.open', new_callable=mock_open)
    def test_process_loghub(self, mock_file, mock_read_csv):
        mock_data = pd.DataFrame([{
            'Date': '2023-01-01',
            'Time': '12:00:00',
            'Content': 'User admin logged in',
            'Level': 'INFO',
            'Component': 'Auth',
            'LineId': '123'
        }])
        mock_read_csv.return_value = mock_data

        process_loghub('dummy_input.csv', 'dummy_output.jsonl')

        mock_file.assert_called_once_with('dummy_output.jsonl', 'w', encoding='utf-8')
        
        handle = mock_file()
        written_args = [call.args[0] for call in handle.write.call_args_list]
        written_json = json.loads(written_args[0].strip())

        self.assertEqual(written_json['@timestamp'], '2023-01-01 12:00:00')
        self.assertEqual(written_json['message'], 'User admin logged in')
        self.assertEqual(written_json['log']['level'], 'INFO')
        self.assertEqual(written_json['log']['logger'], 'Auth')
        self.assertEqual(written_json['event']['id'], 'dummy_input_123')

    @patch('glob.glob')
    @patch('os.path.isdir')
    @patch('pandas.read_csv')
    @patch('builtins.open', new_callable=mock_open)
    def test_process_loghub_interleaving(self, mock_file, mock_read_csv, mock_isdir, mock_glob):
        mock_isdir.return_value = True
        mock_glob.return_value = ['/path/to/Apache_sample.csv', '/path/to/HDFS_sample.csv']
        
        df_apache = pd.DataFrame([{
            'Date': '2023-01-01',
            'Time': '12:00:00',
            'Content': 'Apache log message',
            'Level': 'INFO',
            'Component': 'Web',
            'LineId': '1'
        }])
        df_hdfs = pd.DataFrame([{
            'Date': '2023-01-01',
            'Time': '12:00:01',
            'Content': 'HDFS log message',
            'Level': 'WARN',
            'Component': 'NameNode',
            'LineId': '2'
        }])
        
        mock_read_csv.side_effect = [df_apache, df_hdfs]

        process_loghub('/path/to/dummy_dir', 'dummy_output.jsonl')

        handle = mock_file()
        written_args = [call.args[0] for call in handle.write.call_args_list]
        self.assertEqual(len(written_args), 2)
        
        j1 = json.loads(written_args[0].strip())
        j2 = json.loads(written_args[1].strip())
        
        self.assertEqual(j1['event']['id'], 'Apache_1')
        self.assertEqual(j1['message'], 'Apache log message')
        
        self.assertEqual(j2['event']['id'], 'HDFS_2')
        self.assertEqual(j2['message'], 'HDFS log message')

    @patch('pandas.read_csv')
    @patch('builtins.open', new_callable=mock_open)
    def test_process_botsv3(self, mock_file, mock_read_csv):
        mock_data = pd.DataFrame([{
            '_time': '2023-01-01T12:00:00Z',
            '_raw': 'Failed login attempt',
            'sourcetype': 'winlog',
            'host': 'server-01'
        }])
        mock_read_csv.return_value = mock_data

        process_botsv3('dummy_input.csv', 'dummy_output.jsonl')

        mock_file.assert_called_once_with('dummy_output.jsonl', 'w', encoding='utf-8')
        
        handle = mock_file()
        written_args = [call.args[0] for call in handle.write.call_args_list]
        written_json = json.loads(written_args[0].strip())

        self.assertEqual(written_json['@timestamp'], '2023-01-01T12:00:00Z')
        self.assertEqual(written_json['message'], 'Failed login attempt')
        self.assertEqual(written_json['event']['dataset'], 'winlog')
        self.assertEqual(written_json['host']['name'], 'server-01')

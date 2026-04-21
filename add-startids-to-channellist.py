import pandas as pd
import json
import argparse

'''
Arguments
'''
parser = argparse.ArgumentParser(description='Arguments.')
parser.add_argument(
	'--batch-file',
	type=str,
	required=True,
	help='Specify the filename of the batch file.'
)

# Parse the arguments
args = vars(parser.parse_args())
batch_file = args['batch_file']

# For each channel name, read the json file and get the highest id
# The file does not contain headers, so we need to specify them
headers = ['channel_name', 'max_id']
df = pd.read_csv(batch_file, sep='\t', names=headers)

for i, row in df.iterrows():
    channel_name = row['channel_name']
    path = f'output/data/{channel_name}/{channel_name}_messages.json'
    try:
        with open(path, 'r') as f:
            data = json.load(f)
            messages= data['messages']
        max_id = max([m['id'] for m in messages])
        df.loc[i, 'max_id'] = max_id
    except:
        print(f'Error reading {path}')
        df.loc[i, 'max_id'] = 0
# Remove empty lines
df = df[df['channel_name'].notnull()]

# Save the updated file without headers
df.to_csv(batch_file, sep='\t', index=False, header=False)
print('Done')
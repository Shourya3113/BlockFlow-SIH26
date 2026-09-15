# solver.py
# Your first AI model for the Dadar Train Traffic Control System
# --- VERSION 2: Updated to use 'snapshot.csv' ---

import pandas as pd
from datetime import datetime, timedelta

# --- Step 1: Load all your data from the CSV files ---
# Make sure these CSV files are in the same folder as this script
try:
    df_platforms = pd.read_csv('platforms.csv')
    df_trains = pd.read_csv('trains.csv')
    df_schedule = pd.read_csv('schedule.csv')
    df_snapshot = pd.read_csv('snapshot.csv') # <-- UPDATED FILENAME HERE
    print("✅ All CSV files loaded successfully.")
except FileNotFoundError as e:
    print(f"❌ Error: {e}. Make sure all CSV files are in the right folder.")
    exit()

# --- Step 2: Define simple classes to represent your world ---
# This makes the code cleaner than just using DataFrames everywhere.

class Train:
    def __init__(self, row, master_data):
        self.train_no = row['TrainNo']
        self.line = row['Line']
        self.km_mark = row['KmMark']
        self.status = row['Status']
        # Get master data like priority and type from the other file
        train_info = master_data[master_data['TrainNo'] == self.train_no].iloc[0]
        self.priority = train_info['Priority']
        self.train_type = train_info['TrainType']

    def __repr__(self):
        return f"Train({self.train_no}, P{self.priority}, @{self.km_mark}km on {self.line})"

# --- Step 3: The AI "Brain" - The Heuristic Solver Function ---
# This is the core of your model. It contains the decision-making logic.

def make_ai_decisions(current_trains, schedule, current_time):
    """
    Analyzes the current state of all trains and makes control decisions.
    
    Returns:
        A list of command dictionaries, e.g., [{'train_no': 'X', 'action': 'HOLD', ...}]
    """
    commands = []
    
    # --- RULE 1: Resolve High-Priority Overtake at Platform 3 ---
    # Find trains competing for Platform 3 (DN_FAST track)
    dn_fast_trains_approaching = [
        t for t in current_trains 
        if t.line == 'DN_FAST' and t.km_mark < 8.0 # Approaching Dadar from South
    ]
    
    if len(dn_fast_trains_approaching) > 1:
        # Sort by priority (lowest number is highest priority)
        dn_fast_trains_approaching.sort(key=lambda x: x.priority)
        
        highest_priority_train = dn_fast_trains_approaching[0]
        
        # Check if any lower priority train is ahead of the highest priority one
        for train in dn_fast_trains_approaching[1:]:
            if train.km_mark > highest_priority_train.km_mark:
                command = {
                    'timestamp': current_time.strftime('%H:%M:%S'),
                    'train_to_act': train.train_no,
                    'action': 'HOLD',
                    'location': 'DDR_S_HS (Dadar South Home Signal)',
                    'reason': f"Making way for higher priority train {highest_priority_train.train_no}"
                }
                commands.append(command)

    # --- RULE 2: Manage Platform Clearance for Terminating Train ---
    # Check if the terminating train 'DDR84' is occupying Platform 4
    terminating_train_schedule = schedule[schedule['TrainNo'] == 'DDR84'].iloc[0]
    next_train_schedule = schedule[schedule['TrainNo'] == 'BVI96024'].iloc[0]

    arrival_time_terminating = datetime.strptime(terminating_train_schedule['SchedArrivalDDR'], '%H:%M:%S').time()
    arrival_time_next = datetime.strptime(next_train_schedule['SchedArrivalDDR'], '%H:%M:%S').time()

    # Assuming a 5-minute block time for shunting
    clearance_time = (datetime.combine(datetime.today(), arrival_time_terminating) + timedelta(minutes=5)).time()

    # If the current time is close to the conflict...
    if clearance_time > arrival_time_next and current_time.time() > arrival_time_terminating:
        command = {
            'timestamp': current_time.strftime('%H:%M:%S'),
            'train_to_act': 'BVI96024',
            'action': 'SLOW_DOWN_OR_HOLD',
            'location': 'Approach to DDR_S_HS',
            'reason': f"Platform PF_4 occupied by terminating train {terminating_train_schedule['TrainNo']}. Awaiting clearance."
        }
        commands.append(command)
        
    # --- Add more rules for other conflicts here ---

    if not commands:
        commands.append({
            'timestamp': current_time.strftime('%H:%M:%S'),
            'train_to_act': 'SYSTEM',
            'action': 'MONITOR',
            'location': 'ALL',
            'reason': 'No conflicts detected. Proceeding as per schedule.'
        })
        
    return commands


# --- Step 4: Putting it all together - A sample simulation run ---
if __name__ == "__main__":
    print("\n--- Initializing AI Model Test Run ---")
    
    # Create a list of Train objects from the snapshot data
    initial_trains = [Train(row, df_trains) for index, row in df_snapshot.iterrows()]
    
    print("\nInitial Train Positions:")
    for train in initial_trains:
        print(f"  -> {train}")
        
    # Set the simulation start time from your snapshot
    simulation_time = datetime(2025, 9, 14, 9, 14, 0)
    
    print(f"\n🧠 Running AI Solver at simulation time: {simulation_time.strftime('%H:%M:%S')}...")
    
    # Run the AI brain function with the initial state
    decisions = make_ai_decisions(initial_trains, df_schedule, simulation_time)
    
    print("\n--- AI Decisions ---")
    for decision in decisions:
        print(f"  - COMMAND: {decision['action']} on {decision['train_to_act']}")
        print(f"    -> REASON: {decision['reason']}\n")
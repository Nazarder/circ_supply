import pandas as pd

df = pd.read_csv('cmc_historical_top300_filtered.csv', parse_dates=['snapshot_date'])

# Filter invalid rows to ensure safe division
df = df[df['price'].notna() & (df['price'] > 0) & df['market_cap'].notna()]

df['circulating_supply'] = df['market_cap'] / df['price']

result_df = (
    df[['snapshot_date', 'rank', 'name', 'symbol', 'market_cap', 'price', 'circulating_supply']]
    .sort_values(['snapshot_date', 'rank'])
    .reset_index(drop=True)
)

print(f"Shape: {result_df.shape}")
print(f"NaN in circulating_supply: {result_df['circulating_supply'].isna().sum()}")
print(result_df.head(10).to_string())

result_df.drop(columns=['rank']).to_csv('circulating_supply.csv', index=False)
print("\nSaved to circulating_supply.csv")

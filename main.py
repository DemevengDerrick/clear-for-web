import streamlit as st

# Title------------------------------------------
st.title(
    body= "Clear for Web"
)

st.divider()

# Explanation------------------------------------
st.header(
    body="Introduction"
)

st.text(
    body= "Welcome to Clear for Web, You online application for data cleaning"
)

st.divider()

# inputs-----------------------------------------
st.header(
    body="Input Variables"
)

# file upload
idx_file = st.file_uploader(
    label= "Upload IDX file",
    type= ['idx', 'IDX'],
    accept_multiple_files = False,
    help= "Please upload the idx file that you want to process"
)

appCode = st.selectbox(
    label= "Select a total Station",
    options= ["TCR300", "TCR400", "TCR700", "TCR800", "TS06plus", "Builders"],
    help="Select in the dropdown list below the appratus that was used for the survey."
)

station = st.number_input(
    label= "Station Code",
    step= 1,
    value= 1,
    help= "Code to identify a station record"
)

reference = st.number_input(
    label= "Reference Code",
    step= 1,
    value= 2,
    help= "Code to identify a reference record"
)

measure = st.number_input(
    label= "Measure Code",
    step= 1,
    value= 3,
    help= "Code to identify a measure record"
)

st.button(
    label= "Preview file"
)

@st.cache_data
def run():
    st.text("Ran successfully!")

st.button(
    label= "Run File Cleaning",
    on_click= run
)

st.divider()

#Data Visualization--------------------------
st.header(
    body= "Data Visualization"
)

st.divider()

# Output--------------------------
st.header(
    body= "Outputs"
)

st.download_button(
    label="Download data as CSV",
    data="",
    file_name="large_df.csv",
    mime="text/csv",
)
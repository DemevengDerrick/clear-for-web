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

# total station selection
appCode = st.selectbox(
    label= "Select a total Station",
    options= ["TCR300", "TCR400", "TCR700", "TCR800", "TS06plus", "Builders"],
    help="Select in the dropdown list below the appratus that was used for the survey."
)

# station code selection
station = st.number_input(
    label= "Station Code",
    step= 1,
    value= 1,
    help= "Code to identify a station record"
)
# reference code selection
reference = st.number_input(
    label= "Reference Code",
    step= 1,
    value= 2,
    help= "Code to identify a reference record"
)
# measure code selection
measure = st.number_input(
    label= "Measure Code",
    step= 1,
    value= 3,
    help= "Code to identify a measure record"
)

# data preview button
# Initialize session state for preview content
if "preview_content" not in st.session_state:
    st.session_state.preview_content = ""

# When the Preview button is clicked
if st.button("Raw Data Preview", type="primary"):
    if idx_file is not None:
        # Clear any previous preview
        st.session_state.preview_content = ""
        
        # Read and decode the file content
        content = idx_file.read().decode("utf-8", errors="ignore")
        
        # Update session state with new content
        st.session_state.preview_content = content

    else:
        st.warning("Please upload an IDX file first.")

# Display text area for preview
st.text_area("Data preview", value=st.session_state.preview_content, height=300)

# Clean the data
if st.button(label= "Run File Cleaning", type="primary"):
    pass

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
    type="primary"
)
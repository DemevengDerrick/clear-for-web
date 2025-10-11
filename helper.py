import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import axes3d
import numpy as np
import matplotlib.mlab as ml


def plot():
    pass

def process(fileName, appCode, station, reference, measure): 
    Station=appCode
    CodeStation=station
    CodeReference=reference
    CodeMesur=measure
            
    doc=fileName

    try:
        doc=open(doc)
        if  Station=="":
            Station="1"
        if  CodeStation=="":
            CodeStation="1"
        if  CodeReference=="":
            CodeReference="3"
        if  CodeMesur=="":
            CodeMesur="4"          
    except:
        #output.insert(END,"The file directory could not be found. Enter a valid directory.")
        pass
           
    doc2=open("Temp_result.txt","w")
    
    if int(Station)==1:
                    
        for line in doc:
            if line.startswith("		1,	"):
                break
            
        for line in doc:
           #delete undesired lines 
           if not line.startswith(("	SETUP","		STN_NO","	END","	SLOPE(TgtNo, TgtID, CfgNo, Hz, Vz, SDist, RefHt, Date, Ppm, ApplType, Flags)","END","	SLOPE (TgtNo, TgtID, CfgNo, Hz, Vz, SDist, RefHt, Date, Ppm, ApplType, Flags)")):
              liste=line.split() 
           #delete undesired columns
              if len(liste)==2: 
                  del liste[0]
                  delimiter=''
                  line=delimiter.join(liste)+","
              else:
                  del liste[7:11]
                  del liste[2]
                  del liste[0]
                  delimiter=''
                  line="\n"+delimiter.join(liste)+"\n"
              #print(line) 
              doc2.write(line)
        doc2.close()
         
        doc2=open("Temp_result.txt")
        doc3=asksaveasfile(mode="w" , defaultextension=".txt") 
        print(doc3)
        n=0
        m=0
         
        for lin in doc2:
            if not lin.strip():continue#enleve les lignes vides
            list=lin.split()#decompose les lingnes en liste
            n=n+1
            #print(len(lin))
            #ajout des codes
            if len(lin)<25:
                list=[CodeStation+","]+list
                delimiter=''
                lin=delimiter.join(list)+"\n"
                m=n+1
            elif n==m:
                list=[CodeReference+","]+list
                delimiter=''
                lin=delimiter.join(list)+"\n"
            else:
                 list=[CodeMesur+","]+list
                 delimiter=''
                 lin=delimiter.join(list)+"\n" 
            print(lin)
            output.insert(END,lin)
            doc3.write(lin)
        doc3.close()
        text=str(doc3)
        words=text.split()
        output.insert(END,"-----------------------------------------------------------------------------")
        output.insert(END,".....processing complete. check in your file"+words[1]+" for TXT result......")
#        photo1=PhotoImage(file="good.PNG")
#        Label(window, image=photo1).grid(row=0,column=2, sticky=W)     
    elif int(Station)==2: 
        
        for line in doc:
            if line.startswith("	1	"):
               break
        for line in doc:
        #delete undesired lines
            if not line.startswith(("	SLOPE","	SETUP","	STN_NO","	END","	SLOPE(TgtNo	TgtID	CfgNo	Hz	Vz	SDist	RefHt	Date	Ppm	ApplType	Flags)","END","	SLOPE (TgtNo, TgtID, CfgNo, Hz, Vz, SDist, RefHt, Date, Ppm, ApplType, Flags)")):
                liste=line.split()
        #delete undesired columns
                if len(liste)==2:
                    del liste[0]
                    delimiter=''
                    line=delimiter.join(liste)+","
                else:
                    del liste[7:11]
                    del liste[2]
                    del liste[0]
                    delimiter=","
                    line="\n"+delimiter.join(liste)+"\n" 
                print(line)
                #output.insert(END,line)
                doc2.write(line)
        doc2.close()
        
        doc2=open("Temp_result.txt")
        doc3=asksaveasfile(mode="w" , defaultextension=".txt")
        
        n=0
        m=0
        
        for lin in doc2:
            if not lin.strip():continue#enleve les lignes vides
            list=lin.split()#decompose les lingnes en liste
            n=n+1
            #print(len(lin))
            #ajout des codes
            if len(lin)<25:
                list=[CodeStation+","]+list
                delimiter=''
                lin=delimiter.join(list)+"\n"
                m=n+1
            elif n==m:
                list=[CodeReference+","]+list
                delimiter=''
                lin=delimiter.join(list)+"\n"
            else:
                list=[CodeMesur+","]+list
                delimiter=''
                lin=delimiter.join(list)+"\n"
            print(line)
            output.insert(END,lin)
            doc3.write(lin)
        doc3.close()
        print("--------------------------------------------------------------")
        print(".....processing complete, check in your file location for TXT result......")
        text=str(doc3)
        words=text.split()
        output.insert(END,"-----------------------------------------------------------------------------")
        output.insert(END,".....processing complete. check in your file"+words[1]+" for TXT result......")
		
    else:
        output.insert(END,"Chose a station Code between 1 and 2")

if __name__ == "__main__":
    pass

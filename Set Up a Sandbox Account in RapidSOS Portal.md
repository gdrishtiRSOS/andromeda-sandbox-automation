# **Steps to Set Up a Sandbox Account in RapidSOS Unite**

### **Create the sandbox account**

1. **Register through the sign-up wizard**  
   Go to [sandbox.rapidsosportal.com](https://sandbox.rapidsosportal.com) and select "New Agency Sign Up”. Complete the five steps:  
   **Step 1:** **Register Account** – Enter your RapidSOS alias email with \+ followed by the authority's name. (Example: jdoe+auth123@rapidsos.com)  
   **Step 2: Fill in Agency Info** – Fill in all mandatory fields (Agency Name: Name of the authority in the sandbox).  
   **Step 3: License Agreement** – Add Signature and Accept the terms.  
   **Step 4: Integration Info** – Select "Other" in all fields.  
   **Step 5: Confirmation Email** – You'll receive a confirmation email. Follow only step 1 of the instructions found in the email to verify your address.  
2. **Complete all account details**  
   Go to [andromeda.sandbox.rapidsos.com/authorities](http://andromeda.sandbox.rapidsos.com/authorities) where you will see an account associated with the authority name you used.  
   **Step 1:** Go to the **Account Info** Tab.  
   **Step 2:** Add the Account ID, attach a Dispatch Type, input the Country and State.  
   **Step 3:** Select “Save Changes".

---

### **Downloading and Attaching the Shapefile in Andromeda**

To create the sandbox account in [sandbox.rapidsosportal.com](http://sandbox.rapidsosportal.com) you will need the shapefile associated with the Account ID. In order not to overlap with current existing ECC boundaries you should download and attach your shapefile through Andromeda.

3. **Download the shapefile from Production Andromeda**  
   Before following any steps determine whether the jurisdiction is specific or applies to the entire US.   
   **Step 1:** If the account has a mirrored version in **Andromeda**, retrieve the **Account ID** from there.  
   **Step 2:** Use this **Account ID** to search in JIRA ([JIRA Search](https://rapidsos.atlassian.net/jira/your-work)).  
   **Step 3:** Look for the **Polygon Management Ticket** that contains the shapefile.  
   **Step 4:** Download the **processed version** of the shapefile.  
4. **Attach the shapefile**  
   **Step 1:** Find your authority name in [andromeda.sandbox.rapidsos.com/authorities](http://andromeda.sandbox.rapidsos.com/authorities)  
   **Step 2:** In the **Pending Jurisdiction** section, select **"Select Shapefile"**  
   **Step 3:** Upload the file using the upload button in the top left corner of the displayed map  
   **Step 4:** Set **Ingress Status** to **"Verified"** and **Egress Status** to **"Active"**.  
   **Step 5:** Click **"Create Jurisdiction."**  
   **Step 6:** After creation, change the **Ingress Status** to **"Pending"** and select **"Apply Changes."**

---

### **Adding Integrations and Configuring Capabilities in Andromeda** 

We need to associate the account with a RapidSOS Unite account.

5. **Add integrations**  
   **Step 1:** Find your authority name in [andromeda.sandbox.rapidsos.com/authorities](http://andromeda.sandbox.rapidsos.com/authorities)  
   **Step 2:** In the **Add Integration** section, enter:  
- **App Name:** Authority name followed by **"Sandbox," "RSP," and the date**.  
- **Select a Product:** Name of the integration (**"RapidSOS Portal"**).

Follow a similar process to add the **type of access** required (LEI, LIS, RADE, iRPv2, Alerts, etc).

6. **Configure capabilities**  
   **Step 1:** In the **Configure Capabilities** section, select the integration you want to modify  
   **Step 2:** Ensure you select the correct capabilities based on the type of access.  
- If granting access to **Location APIs**, select **Location capabilities**, if enabling **Alerts**, select **Alerts capabilities**, if enabling **RADE**, select the capability to receive additional data from B2B partners etc.  
- If the account is **primary**, enable **Jurisdiction View**.  
- **Important:** **Alerts capabilities should not be selected** for an account with a **USA geofence** unless it is tied to a **specific jurisdiction**. Otherwise, alerts **will not work** in the sandbox.

---

### **Final Steps**

7. **Activate the jurisdiction**  
   **Step 1:** Go to the **Revision Tab** ([andromeda.sandbox.rapidsos.com/revisions](http://andromeda.sandbox.rapidsos.com/revisions)).  
   **Step 2:** Select **"Create Revision"** and test with several random numbers.  
   **Step 3:** Select **"Publish Revision"** to change the jurisdiction to an active state.  
8. **Activate data sources in the sandbox**  
   **Step 1:** Go back to the **Sandbox RapidSOS Unite and Log in**  
   **Step 2:** Go to **"Admin"** in the menu found in the top right corner ([sandbox.rapidsosportal.com/admin/psap](http://sandbox.rapidsosportal.com/admin/psap)).  
   **Step 3:** Go to the **Role and Access tab** in the Admin menu.  
   **Step 4: Enable** capabilities selected in Andromeda **manually.** By selecting **all capabilities**, we ensure that these data sources are available in the **Role and Access** tab.  
- **Enable all data sources** for both **Admin** and **Agent roles** to provide access to the **Alerts tab** in the sandbox.

